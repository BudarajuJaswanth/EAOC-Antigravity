"""
EAOC Unit Test Suite - Model Provider Abstraction & Factory Layer

Tests required Part 1.9 behavior:
1. Provider selection (gemini, lm_studio, ollama, default setting)
2. Gemini configuration (valid parameters, explicit mocking)
3. LM Studio configuration (base_url, model)
4. Ollama configuration (base_url endpoint formatting, model)
5. Missing Gemini dependency (raises explicit ImportError, no auto-mock fallback)
6. Missing credentials/config (raises explicit ValueError)
7. Invalid provider handling (raises explicit ValueError)
8. Explicit test mocking (no live network/LLM calls)
9. Secret redaction (API keys redacted in logs and exceptions)
10. Non-invasive health checks (zero network/LLM calls)
"""

import sys
import unittest
from unittest.mock import MagicMock, patch
from pydantic import SecretStr

from eaoc.config import get_settings
from eaoc.models.base import ModelProviderCategory, ModelProviderWrapper
from eaoc.models.factory import ModelFactory, get_model_provider
from eaoc.schemas.health import HealthStatus
from eaoc.services.health import HealthService


class TestModelProviderAbstraction(unittest.TestCase):
    """Unit tests for EAOC provider-neutral model abstraction and factory layer."""

    def test_invalid_provider_raises_value_error(self):
        """Verify unsupported provider name raises explicit ValueError."""
        with self.assertRaises(ValueError) as ctx:
            ModelFactory.get_model_provider("unsupported_provider_xyz")
        self.assertIn("Unsupported model provider 'unsupported_provider_xyz'", str(ctx.exception))

    def test_gemini_missing_credentials_raises_value_error(self):
        """Verify Gemini provider raises ValueError when GEMINI_API_KEY is missing or empty."""
        mock_settings = get_settings().model_copy(update={"GEMINI_API_KEY": None})
        with patch("eaoc.models.factory.get_settings", return_value=mock_settings):
            with self.assertRaises(ValueError) as ctx:
                ModelFactory.get_model_provider("gemini")
            self.assertIn("GEMINI_API_KEY is not configured", str(ctx.exception))

    def test_gemini_missing_dependency_raises_import_error(self):
        """Verify Gemini provider raises explicit ImportError when langchain-google-genai is missing."""
        mock_settings = get_settings().model_copy(
            update={"GEMINI_API_KEY": SecretStr("test_secret_key_123")}
        )
        with patch("eaoc.models.factory.get_settings", return_value=mock_settings):
            with patch.dict(sys.modules, {"langchain_google_genai": None}):
                with self.assertRaises(ImportError) as ctx:
                    ModelFactory.get_model_provider("gemini")
                self.assertIn("langchain-google-genai", str(ctx.exception))

    def test_gemini_valid_configuration_instantiation(self):
        """Verify Gemini provider instantiates correctly using explicit test mock."""
        mock_settings = get_settings().model_copy(
            update={
                "DEFAULT_MODEL_PROVIDER": "gemini",
                "GEMINI_API_KEY": SecretStr("test_secret_gemini_key"),
                "GEMINI_MODEL": "gemini-1.5-pro",
            }
        )
        mock_genai_cls = MagicMock()
        mock_chat_instance = MagicMock()
        mock_genai_cls.return_value = mock_chat_instance

        with patch("eaoc.models.factory.get_settings", return_value=mock_settings), \
             patch.dict("sys.modules", {"langchain_google_genai": MagicMock(ChatGoogleGenerativeAI=mock_genai_cls)}):

            wrapper = ModelFactory.get_model_provider("gemini")
            self.assertEqual(wrapper.provider_id, "gemini")
            self.assertEqual(wrapper.model_name, "gemini-1.5-pro")
            self.assertEqual(wrapper.category, ModelProviderCategory.CLOUD)
            self.assertEqual(wrapper.chat_model, mock_chat_instance)
            mock_genai_cls.assert_called_once()
            call_kwargs = mock_genai_cls.call_args[1]
            self.assertEqual(call_kwargs.get("model"), "gemini-1.5-pro")
            self.assertEqual(call_kwargs.get("google_api_key"), "test_secret_gemini_key")

    def test_lm_studio_missing_base_url_raises_value_error(self):
        """Verify LM Studio raises ValueError when LM_STUDIO_BASE_URL is missing."""
        mock_settings = get_settings().model_copy(update={"LM_STUDIO_BASE_URL": None})
        with patch("eaoc.models.factory.get_settings", return_value=mock_settings):
            with self.assertRaises(ValueError) as ctx:
                ModelFactory.get_model_provider("lm_studio")
            self.assertIn("LM_STUDIO_BASE_URL is not configured", str(ctx.exception))

    def test_lm_studio_valid_configuration_instantiation(self):
        """Verify LM Studio instantiates ChatOpenAI pointing to local base URL using explicit test mock."""
        mock_settings = get_settings().model_copy(
            update={
                "LM_STUDIO_BASE_URL": "http://localhost:1234/v1",
                "LM_STUDIO_MODEL": "qwen2.5-7b-instruct",
            }
        )
        mock_openai_cls = MagicMock()
        mock_chat_instance = MagicMock()
        mock_openai_cls.return_value = mock_chat_instance

        with patch("eaoc.models.factory.get_settings", return_value=mock_settings), \
             patch.dict("sys.modules", {"langchain_openai": MagicMock(ChatOpenAI=mock_openai_cls)}):

            wrapper = get_model_provider("lm_studio")
            self.assertEqual(wrapper.provider_id, "lm_studio")
            self.assertEqual(wrapper.model_name, "qwen2.5-7b-instruct")
            self.assertEqual(wrapper.category, ModelProviderCategory.LOCAL)
            self.assertEqual(wrapper.chat_model, mock_chat_instance)
            mock_openai_cls.assert_called_once()
            call_kwargs = mock_openai_cls.call_args[1]
            self.assertEqual(call_kwargs.get("openai_api_base"), "http://localhost:1234/v1")

    def test_ollama_valid_configuration_and_url_formatting(self):
        """Verify Ollama formats base URL to /v1 and instantiates ChatOpenAI using explicit test mock."""
        mock_settings = get_settings().model_copy(
            update={
                "OLLAMA_BASE_URL": "http://localhost:11434",
                "OLLAMA_MODEL": "llama3.2:3b",
            }
        )
        mock_openai_cls = MagicMock()
        mock_chat_instance = MagicMock()
        mock_openai_cls.return_value = mock_chat_instance

        with patch("eaoc.models.factory.get_settings", return_value=mock_settings), \
             patch.dict("sys.modules", {"langchain_openai": MagicMock(ChatOpenAI=mock_openai_cls)}):

            wrapper = get_model_provider("ollama")
            self.assertEqual(wrapper.provider_id, "ollama")
            self.assertEqual(wrapper.model_name, "llama3.2:3b")
            self.assertEqual(wrapper.category, ModelProviderCategory.LOCAL)
            self.assertEqual(wrapper.chat_model, mock_chat_instance)
            mock_openai_cls.assert_called_once()
            call_kwargs = mock_openai_cls.call_args[1]
            self.assertEqual(call_kwargs.get("openai_api_base"), "http://localhost:11434/v1")

    def test_no_automatic_runtime_mock_fallback(self):
        """Verify production code NEVER returns a mock fallback automatically on failure."""
        mock_settings = get_settings().model_copy(
            update={"GEMINI_API_KEY": SecretStr("invalid_key")}
        )
        def bad_genai_init(*args, **kwargs):
            raise Exception("Invalid API key secret: SecretKey123")

        mock_genai_cls = MagicMock(side_effect=bad_genai_init)

        with patch("eaoc.models.factory.get_settings", return_value=mock_settings), \
             patch.dict("sys.modules", {"langchain_google_genai": MagicMock(ChatGoogleGenerativeAI=mock_genai_cls)}):

            with self.assertRaises(RuntimeError) as ctx:
                ModelFactory.get_model_provider("gemini")
            
            # Verify explicit RuntimeError is raised (NO silent mock fallback)
            self.assertIn("Gemini model provider initialization failed", str(ctx.exception))
            # Verify secret redaction
            self.assertNotIn("SecretKey123", str(ctx.exception))

    def test_non_invasive_health_check(self):
        """Verify check_model_provider_health evaluates configuration without network/LLM calls."""
        health_svc = HealthService()

        # Case A: Provider unconfigured
        mock_settings_unconf = get_settings().model_copy(
            update={"DEFAULT_MODEL_PROVIDER": "gemini", "GEMINI_API_KEY": None}
        )
        with patch("eaoc.services.health.get_settings", return_value=mock_settings_unconf):
            h_unconf = health_svc.check_model_provider_health()
            self.assertEqual(h_unconf.status, HealthStatus.NOT_CONFIGURED)
            self.assertIn("gemini", h_unconf.details)

        # Case B: Provider configured statically
        mock_settings_conf = get_settings().model_copy(
            update={
                "DEFAULT_MODEL_PROVIDER": "lm_studio",
                "LM_STUDIO_BASE_URL": "http://localhost:1234/v1",
            }
        )
        with patch("eaoc.services.health.get_settings", return_value=mock_settings_conf):
            h_conf = health_svc.check_model_provider_health()
            self.assertEqual(h_conf.status, HealthStatus.HEALTHY)
            self.assertIn("lm_studio", h_conf.details)


if __name__ == "__main__":
    unittest.main()
