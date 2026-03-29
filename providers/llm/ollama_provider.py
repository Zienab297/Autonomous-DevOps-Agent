"""
providers/llm/ollama_provider.py
----------------------------------
Ollama local LLM provider — no API key needed.
"""

from providers.llm.base_llm_provider import BaseLLMProvider, LLMResponse


class OllamaProvider(BaseLLMProvider):

    name = "ollama"

    DEFAULT_MODELS = [
        "llama3.2:3b",
        "llama3.1:8b",
        "mistral:7b",
        "gemma2:9b",
        "phi3:mini",
    ]

    def __init__(self, model: str = "llama3.2:3b"):
        self.default_model = model

    def chat(self, messages: list[dict], model: str = None, **kwargs) -> LLMResponse:
        import ollama
        model = model or self.default_model
        response = ollama.chat(model=model, messages=messages)
        return LLMResponse(
            content  = response["message"]["content"],
            model    = model,
            provider = self.name,
        )

    def is_available(self) -> bool:
        try:
            import ollama
            ollama.list()
            return True
        except Exception:
            return False

    def list_models(self) -> list[str]:
        try:
            import ollama
            models = ollama.list()
            names  = [m["name"] for m in models.get("models", [])]
            return names if names else self.DEFAULT_MODELS
        except Exception:
            return self.DEFAULT_MODELS