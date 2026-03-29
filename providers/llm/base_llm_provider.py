"""
providers/llm/base_llm_provider.py
------------------------------------
Abstract base class for all LLM providers.
Every provider must implement chat() and is_available().
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class LLMResponse:
    content: str
    model  : str
    provider: str


class BaseLLMProvider(ABC):

    name: str = "base"

    @abstractmethod
    def chat(self, messages: list[dict], model: str, **kwargs) -> LLMResponse:
        """
        Send messages to the LLM and return a response.

        Args:
            messages: list of {"role": "user"/"assistant"/"system", "content": "..."}
            model:    model name to use
            kwargs:   extra params (temperature, max_tokens, etc.)

        Returns:
            LLMResponse
        """

    @abstractmethod
    def is_available(self) -> bool:
        """Check if the provider is reachable (ping test)."""

    @abstractmethod
    def list_models(self) -> list[str]:
        """Return available model names for this provider."""