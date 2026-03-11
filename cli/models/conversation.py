from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional


@dataclass
class Message:
    role: str
    content: Optional[str] = None
    tool_calls: Optional[list] = None      # For assistant messages with tool calls
    tool_call_id: Optional[str] = None     # For tool result messages
    name: Optional[str] = None             # Tool name for tool results


class ConversationModel:
    """
    Manages full conversation history including tool calls and results.
    Supports the agentic loop: user → assistant (tool call) → tool result → assistant → ...
    """

    def __init__(self, system_prompt: str = ""):
        self._history: List[Message] = []
        self.system_prompt = system_prompt
        self.turn_count = 0

    def add_user_message(self, content: str):
        self._history.append(Message(role="user", content=content))
        self.turn_count += 1

    def add_assistant_message(self, content: str = None, tool_calls=None):
        self._history.append(Message(role="assistant", content=content, tool_calls=tool_calls))

    def add_tool_result(self, tool_call_id: str, tool_name: str, result: str):
        self._history.append(Message(
            role="tool",
            content=result,
            tool_call_id=tool_call_id,
            name=tool_name,
        ))

    def to_api_format(self) -> List[Dict[str, Any]]:
        """Convert history to Groq API format."""
        messages = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})

        for msg in self._history:
            if msg.role == "user":
                messages.append({"role": "user", "content": msg.content})

            elif msg.role == "assistant":
                entry = {"role": "assistant"}
                if msg.content:
                    entry["content"] = msg.content
                if msg.tool_calls:
                    entry["tool_calls"] = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in msg.tool_calls
                    ]
                messages.append(entry)

            elif msg.role == "tool":
                messages.append({
                    "role": "tool",
                    "tool_call_id": msg.tool_call_id,
                    "content": msg.content,
                })

        return messages

    def clear(self):
        self._history = []
        self.turn_count = 0

    def last_messages(self, n: int = 5) -> List[Message]:
        return self._history[-n:]