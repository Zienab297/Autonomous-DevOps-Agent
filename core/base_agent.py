from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any
import logging

logger = logging.getLogger(__name__)


@dataclass
class AgentEvent:
    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    source: str = "unknown"
    correlation_id: str | None = None


class BaseAgent(ABC):
    def __init__(self, name: str):
        self.name = name
        self._running = False
        self.logger = logging.getLogger(f"agent.{name}")

    @abstractmethod
    async def handle_event(self, event: AgentEvent) -> Any:
        """Handle an incoming event."""

    async def start(self) -> None:
        self._running = True
        self.logger.info(f"Agent '{self.name}' started")

    async def stop(self) -> None:
        self._running = False
        self.logger.info(f"Agent '{self.name}' stopped")

    @property
    def is_running(self) -> bool:
        return self._running