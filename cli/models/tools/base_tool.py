from abc import ABC, abstractmethod
from typing import Any


class BaseTool(ABC):
    """
    Base class for all agent tools.
    Add new tools by subclassing this and registering in executor.py.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Tool name — must match what the LLM calls."""
        pass

    @property
    @abstractmethod
    def description(self) -> str:
        """Describes what the tool does (shown to the LLM)."""
        pass

    @property
    @abstractmethod
    def parameters(self) -> dict:
        """JSON schema of the tool's parameters."""
        pass

    @abstractmethod
    def run(self, **kwargs) -> str:
        """Execute the tool and return a string result."""
        pass

    def to_groq_schema(self) -> dict:
        """Convert to Groq tool call format."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }