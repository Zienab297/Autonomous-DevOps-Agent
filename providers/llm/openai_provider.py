"""
providers/llm/openai_provider.py
"""
from providers.llm.base_llm_provider import BaseLLMProvider, LLMResponse


class OpenAIProvider(BaseLLMProvider):

    name = "openai"

    DEFAULT_MODELS = [
        "gpt-4o",
        "gpt-4o-mini",
        "gpt-4-turbo",
        "gpt-3.5-turbo",
    ]

    def __init__(self, api_key: str, model: str = "gpt-4o"):
        self.api_key       = api_key
        self.default_model = model

    def chat(self, messages: list[dict], model: str = None, **kwargs) -> LLMResponse:
        from openai import OpenAI
        model  = model or self.default_model
        client = OpenAI(api_key=self.api_key)
        resp   = client.chat.completions.create(
            model      = model,
            messages   = messages,
            max_tokens = kwargs.get("max_tokens", 4096),
        )
        return LLMResponse(
            content  = resp.choices[0].message.content or "",
            model    = model,
            provider = self.name,
        )

    def is_available(self) -> bool:
        try:
            from openai import OpenAI
            OpenAI(api_key=self.api_key).models.list()
            return True
        except Exception:
            return False

    def list_models(self) -> list[str]:
        try:
            from openai import OpenAI
            models = OpenAI(api_key=self.api_key).models.list()
            gpt = sorted([m.id for m in models.data if "gpt" in m.id])
            return gpt if gpt else self.DEFAULT_MODELS
        except Exception:
            return self.DEFAULT_MODELS