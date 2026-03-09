from groq import Groq
from models.conversation import ConversationModel
import config


class AIModel:
    """
    Handles all communication with the Groq API.
    Swap this class out to change providers (OpenAI, Ollama, Anthropic, etc.)
    """

    def __init__(self):
        self.client = Groq(api_key=config.GROQ_API_KEY)
        self.model = config.MODEL
        self.max_tokens = config.MAX_TOKENS

    def get_response(self, conversation: ConversationModel) -> str:
        """Send conversation history to Groq and return the assistant's reply."""
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                max_tokens=self.max_tokens,
                messages=conversation.to_api_format(),
            )
            return response.choices[0].message.content

        except Exception as e:
            # Surface clean errors to the controller
            raise RuntimeError(f"API call failed: {e}")