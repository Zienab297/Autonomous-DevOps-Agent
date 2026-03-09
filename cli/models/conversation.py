from dataclasses import dataclass, field
from typing import List, Dict


@dataclass
class Message:
    role: str   # "user" | "assistant" | "system"
    content: str


class ConversationModel:
    """
    Manages conversation history and state.
    Extend this to add: persistence, summarization, context windowing, etc.
    """

    def __init__(self, system_prompt: str = ""):
        self._history: List[Message] = []
        self.system_prompt = system_prompt
        self.turn_count = 0

    def add_user_message(self, content: str):
        self._history.append(Message(role="user", content=content))
        self.turn_count += 1

    def add_assistant_message(self, content: str):
        self._history.append(Message(role="assistant", content=content))

    def to_api_format(self) -> List[Dict[str, str]]:
        """Convert history to the format expected by the Groq/OpenAI API."""
        messages = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        for msg in self._history:
            messages.append({"role": msg.role, "content": msg.content})
        return messages

    def clear(self):
        self._history = []
        self.turn_count = 0

    def get_history(self) -> List[Message]:
        return list(self._history)