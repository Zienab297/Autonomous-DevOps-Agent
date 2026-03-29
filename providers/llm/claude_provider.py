"""
providers/llm/claude_provider.py
----------------------------------
Anthropic Claude cloud LLM provider — requires ANTHROPIC_API_KEY.
"""

from providers.llm.base_llm_provider import BaseLLMProvider, LLMResponse


class ClaudeProvider(BaseLLMProvider):

    name = "claude"

    DEFAULT_MODELS = [
        "claude-opus-4-6",
        "claude-sonnet-4-6",
        "claude-haiku-4-5-20251001",
    ]

    def __init__(self, api_key: str, model: str = "claude-sonnet-4-6"):
        self.api_key       = api_key
        self.default_model = model

    def chat(self, messages: list[dict], model: str = None, **kwargs) -> LLMResponse:
        import anthropic
        model  = model or self.default_model
        client = anthropic.Anthropic(api_key=self.api_key)

        # Anthropic separates system from messages
        system_msg = ""
        chat_msgs  = []
        for m in messages:
            if m["role"] == "system":
                system_msg = m["content"]
            else:
                chat_msgs.append(m)

        resp = client.messages.create(
            model      = model,
            max_tokens = kwargs.get("max_tokens", 4096),
            system     = system_msg,
            messages   = chat_msgs,
        )
        return LLMResponse(
            content  = resp.content[0].text,
            model    = model,
            provider = self.name,
        )

    def is_available(self) -> bool:
        try:
            import anthropic
            anthropic.Anthropic(api_key=self.api_key).models.list()
            return True
        except Exception:
            return False

    def list_models(self) -> list[str]:
        return self.DEFAULT_MODELS