from groq import Groq
from devops_agent.models.conversation import ConversationModel
import config


class AIModel:
    """
    Handles all Groq API calls.
    Supports both plain responses and tool call responses for the agentic loop.
    """

    def __init__(self, tools: list = None):
        self.client = Groq(api_key=config.GROQ_API_KEY)
        self.model = config.MODEL
        self.max_tokens = config.MAX_TOKENS
        self.tools = tools or []  # Tool schemas passed in from executor

    def get_response(self, conversation: ConversationModel):
        """
        Call the LLM. Returns the full response object.
        Caller checks response.choices[0].message.tool_calls to detect tool use.
        """
        try:
            kwargs = {
                "model": self.model,
                "max_tokens": self.max_tokens,
                "messages": conversation.to_api_format(),
            }
            if self.tools:
                kwargs["tools"] = self.tools

            response = self.client.chat.completions.create(**kwargs)
            return response

        except Exception as e:
            raise RuntimeError(f"API call failed: {e}")