"""
EAOC Model Provider Factory
Instantiates provider-neutral LangChain chat models for Gemini, LM Studio, and Ollama.
Strictly enforces no automatic runtime mock fallbacks, explicit dependency checks,
and secret-safe diagnostic logging.
"""

import logging
from typing import Optional

from eaoc.config import get_settings
from eaoc.models.base import ModelProviderCategory, ModelProviderWrapper
from eaoc.observability import redact_sensitive_data

logger = logging.getLogger("eaoc.models.factory")


class ModelFactory:
    """
    Factory for resolving and instantiating provider-neutral LangChain BaseChatModel instances.
    """

    @staticmethod
    def get_model_provider(provider_name: Optional[str] = None) -> ModelProviderWrapper:
        """
        Instantiates a ModelProviderWrapper for the target provider.
        If provider_name is omitted, defaults to get_settings().DEFAULT_MODEL_PROVIDER.

        Raises:
            ValueError: If provider is unsupported or required configuration settings are missing.
            ImportError: If required provider library (e.g. langchain-google-genai) is not installed.
            RuntimeError: If model initialization fails due to runtime errors.
        """
        settings = get_settings()
        target_provider = (provider_name or settings.DEFAULT_MODEL_PROVIDER).lower().strip()

        if target_provider == "gemini":
            return ModelFactory._create_gemini_provider(settings)
        elif target_provider == "lm_studio":
            return ModelFactory._create_lm_studio_provider(settings)
        elif target_provider == "ollama":
            return ModelFactory._create_ollama_provider(settings)
        else:
            raise ValueError(
                f"Unsupported model provider '{target_provider}'. "
                "Approved choices: gemini, lm_studio, ollama."
            )

    @staticmethod
    def _create_gemini_provider(settings) -> ModelProviderWrapper:
        """Creates Gemini cloud model wrapper using ChatGoogleGenerativeAI."""
        if not settings.GEMINI_API_KEY:
            raise ValueError(
                "GEMINI_API_KEY is not configured in settings. "
                "Set GEMINI_API_KEY in environment or .env file."
            )
        
        api_key_str = settings.GEMINI_API_KEY.get_secret_value() if hasattr(settings.GEMINI_API_KEY, "get_secret_value") else str(settings.GEMINI_API_KEY)
        if not api_key_str or not api_key_str.strip():
            raise ValueError("GEMINI_API_KEY is empty or invalid.")

        try:
            from langchain_google_genai import ChatGoogleGenerativeAI
        except ImportError as err:
            raise ImportError(
                "Required provider package 'langchain-google-genai' is not installed. "
                "Please install it to use the 'gemini' provider."
            ) from err

        model_name = settings.GEMINI_MODEL or "gemini-1.5-pro"
        try:
            chat_model = ChatGoogleGenerativeAI(
                model=model_name,
                google_api_key=api_key_str,
                temperature=0.0,
            )
            wrapper = ModelProviderWrapper(
                provider_id="gemini",
                model_name=model_name,
                category=ModelProviderCategory.CLOUD,
                chat_model=chat_model,
            )
            logger.info(f"Initialized model provider 'gemini' with model '{model_name}'.")
            return wrapper
        except Exception as exc:
            safe_err = redact_sensitive_data(exc)
            logger.error(f"Failed to initialize Gemini provider: {safe_err}")
            raise RuntimeError(f"Gemini model provider initialization failed: {safe_err}") from exc

    @staticmethod
    def _create_lm_studio_provider(settings) -> ModelProviderWrapper:
        """Creates LM Studio local model wrapper using ChatOpenAI."""
        base_url = settings.LM_STUDIO_BASE_URL
        if not base_url or not base_url.strip():
            raise ValueError(
                "LM_STUDIO_BASE_URL is not configured in settings. "
                "Set LM_STUDIO_BASE_URL in environment or .env file."
            )

        try:
            from langchain_openai import ChatOpenAI
        except ImportError as err:
            raise ImportError(
                "Required provider package 'langchain-openai' is not installed. "
                "Please install it to use the 'lm_studio' provider."
            ) from err

        model_name = settings.LM_STUDIO_MODEL or "qwen2.5-7b-instruct"
        try:
            chat_model = ChatOpenAI(
                model=model_name,
                openai_api_base=base_url,
                openai_api_key="lm-studio",
                temperature=0.0,
            )
            wrapper = ModelProviderWrapper(
                provider_id="lm_studio",
                model_name=model_name,
                category=ModelProviderCategory.LOCAL,
                chat_model=chat_model,
            )
            logger.info(f"Initialized model provider 'lm_studio' with model '{model_name}'.")
            return wrapper
        except Exception as exc:
            safe_err = redact_sensitive_data(exc)
            logger.error(f"Failed to initialize LM Studio provider: {safe_err}")
            raise RuntimeError(f"LM Studio model provider initialization failed: {safe_err}") from exc

    @staticmethod
    def _create_ollama_provider(settings) -> ModelProviderWrapper:
        """Creates Ollama local model wrapper using ChatOpenAI or ChatOllama."""
        base_url = settings.OLLAMA_BASE_URL
        if not base_url or not base_url.strip():
            raise ValueError(
                "OLLAMA_BASE_URL is not configured in settings. "
                "Set OLLAMA_BASE_URL in environment or .env file."
            )

        try:
            from langchain_openai import ChatOpenAI
        except ImportError as err:
            raise ImportError(
                "Required provider package 'langchain-openai' is not installed. "
                "Please install it to use the 'ollama' provider."
            ) from err

        formatted_url = base_url.rstrip("/")
        if not formatted_url.endswith("/v1"):
            formatted_url = f"{formatted_url}/v1"

        model_name = settings.OLLAMA_MODEL or "llama3.2:3b"
        try:
            chat_model = ChatOpenAI(
                model=model_name,
                openai_api_base=formatted_url,
                openai_api_key="ollama",
                temperature=0.0,
            )
            wrapper = ModelProviderWrapper(
                provider_id="ollama",
                model_name=model_name,
                category=ModelProviderCategory.LOCAL,
                chat_model=chat_model,
            )
            logger.info(f"Initialized model provider 'ollama' with model '{model_name}'.")
            return wrapper
        except Exception as exc:
            safe_err = redact_sensitive_data(exc)
            logger.error(f"Failed to initialize Ollama provider: {safe_err}")
            raise RuntimeError(f"Ollama model provider initialization failed: {safe_err}") from exc


def get_model_provider(provider_name: Optional[str] = None) -> ModelProviderWrapper:
    """Convenience function returning a ModelProviderWrapper instance."""
    return ModelFactory.get_model_provider(provider_name)
