"""
EAOC Model Provider Base Abstraction
Defines provider categories and standard container wrapper for LangChain chat models.
"""

from enum import Enum
from typing import Any, Dict
from pydantic import BaseModel, ConfigDict
from langchain_core.language_models.chat_models import BaseChatModel


class ModelProviderCategory(str, Enum):
    """Classification category for model serving location."""
    LOCAL = "LOCAL"
    CLOUD = "CLOUD"


class ModelProviderWrapper(BaseModel):
    """
    Standard container wrapping a provider-agnostic LangChain BaseChatModel.
    Downstream agents consume chat_model for state graph node execution.
    """
    model_config = ConfigDict(arbitrary_types_allowed=True)

    provider_id: str
    model_name: str
    category: ModelProviderCategory
    chat_model: Any  # BaseChatModel instance (or test mock)

    def get_info(self) -> Dict[str, Any]:
        """Returns safe, non-sensitive provider metadata for diagnostic logging."""
        return {
            "provider_id": self.provider_id,
            "model_name": self.model_name,
            "category": self.category.value,
        }
