"""
providers/llm/gemini_provider.py
----------------------------------
Google Gemini cloud LLM provider — requires GEMINI_API_KEY.

Install: pip install google-generativeai
"""

from providers.llm.base_llm_provider import BaseLLMProvider, LLMResponse


class GeminiProvider(BaseLLMProvider):

    name = "gemini"

    DEFAULT_MODELS = [
        "gemini-2.0-flash",
        "gemini-1.5-pro",
        "gemini-1.5-flash",
    ]

    def __init__(self, api_key: str, model: str = "gemini-2.0-flash"):
        self.api_key       = api_key
        self.default_model = model
        self._check_package()

    @staticmethod
    def _check_package():
        try:
            import google.generativeai
        except ImportError:
            raise ImportError(
                "google-generativeai is not installed.\n"
                "Run: pip install google-generativeai"
            )

    def chat(self, messages: list[dict], model: str = None, **kwargs) -> LLMResponse:
        try:
            import google.generativeai as genai
        except ImportError:
            raise ImportError("Run: pip install google-generativeai")

        model = model or self.default_model
        genai.configure(api_key=self.api_key)

        system_parts = [m["content"] for m in messages if m["role"] == "system"]
        history      = []
        last_user    = ""

        for m in messages:
            if m["role"] == "system":
                continue
            elif m["role"] == "user":
                last_user = m["content"]
                history.append({"role": "user", "parts": [m["content"]]})
            elif m["role"] == "assistant":
                history.append({"role": "model", "parts": [m["content"]]})

        system_instruction = "\n".join(system_parts) if system_parts else None
        client = genai.GenerativeModel(
            model_name         = model,
            system_instruction = system_instruction,
        )
        chat_session = client.start_chat(history=history[:-1] if len(history) > 1 else [])
        resp         = chat_session.send_message(last_user or (history[-1]["parts"][0] if history else ""))

        return LLMResponse(
            content  = resp.text,
            model    = model,
            provider = self.name,
        )

    def is_available(self) -> bool:
        try:
            import google.generativeai as genai
            genai.configure(api_key=self.api_key)
            list(genai.list_models())
            return True
        except Exception:
            return False

    def list_models(self) -> list[str]:
        try:
            import google.generativeai as genai
            genai.configure(api_key=self.api_key)
            return [
                m.name.replace("models/", "")
                for m in genai.list_models()
                if "generateContent" in m.supported_generation_methods
            ]
        except Exception:
            return self.DEFAULT_MODELS