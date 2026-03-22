"""
BaseAgent - Abstract Foundation for All Agents
================================================
Every agent in the system subclasses this.
Provides lifecycle management, event handling,
registry integration, heartbeat, and error recovery.
"""

import asyncio
import logging
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from core.event_bus import EventBus, Event, EventType
    from core.agent_registery import AgentRegistry, AgentStatus

logger = logging.getLogger(__name__)


# ============================================================
# AgentEvent — Internal event passed to handle_event()
# ============================================================

@dataclass
class AgentEvent:
    """
    The event object received by every agent's handle_event().

    Example:
        AgentEvent(
            type="remediation.started",
            payload={"service": "auth-api", "action": "restart_service"},
            source="orchestrator",
            correlation_id="INC-A1B2C3"
        )
    """
    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    source: str = "unknown"
    correlation_id: str | None = None
    timestamp: datetime = field(default_factory=datetime.utcnow)
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def __str__(self):
        return (
            f"AgentEvent(type={self.type}, "
            f"source={self.source}, "
            f"correlation_id={self.correlation_id})"
        )


# ============================================================
# AgentState — Lifecycle states
# ============================================================

class AgentState(str, Enum):
    IDLE     = "idle"      # Created but not started
    STARTING = "starting"  # In the process of starting
    RUNNING  = "running"   # Active and processing events
    STOPPING = "stopping"  # In the process of stopping
    STOPPED  = "stopped"   # Cleanly shut down
    ERROR    = "error"     # Crashed or failed to start


# ============================================================
# BaseAgent
# ============================================================

class BaseAgent(ABC):
    """
    Abstract base class for all agents in the system.

    Subclass this to create a new agent:

        class MonitoringAgent(BaseAgent):
            def __init__(self, bus, registry):
                super().__init__(
                    name="monitoring_agent",
                    event_bus=bus,
                    registry=registry,
                )

            async def handle_event(self, event: AgentEvent) -> Any:
                if event.type == "task.monitor":
                    await self._check_metrics()

            async def _setup(self):
                # subscribe to events here
                self.subscribe(EventType.METRICS_COLLECTED, self.handle_event)

            async def _teardown(self):
                # clean up resources here
                pass

    Lifecycle:
        await agent.start()   ->  _setup() called, state = RUNNING
        await agent.stop()    ->  _teardown() called, state = STOPPED
    """

    def __init__(
        self,
        name: str,
        event_bus: Optional["EventBus"] = None,
        registry: Optional["AgentRegistry"] = None,
        heartbeat_interval: float = 30.0,
    ):
        self.name = name
        self.agent_id = f"{name}-{str(uuid.uuid4())[:8]}"
        self.logger = logging.getLogger(f"agent.{name}")

        # Core dependencies (optional — can be set later)
        self._bus = event_bus
        self._registry = registry

        # State
        self._state = AgentState.IDLE
        self._started_at: Optional[datetime] = None
        self._error: Optional[Exception] = None

        # Heartbeat
        self._heartbeat_interval = heartbeat_interval
        self._heartbeat_task: Optional[asyncio.Task] = None

        # Event subscriptions registered via self.subscribe()
        self._subscriptions: list[tuple] = []

    # --------------------------------------------------------
    # Abstract methods — must be implemented by subclasses
    # --------------------------------------------------------

    @abstractmethod
    async def handle_event(self, event: AgentEvent) -> Any:
        """
        Handle an incoming event.
        Called whenever a subscribed event is published on the bus.

        Args:
            event: The AgentEvent to process

        Returns:
            Any result (optional, used for direct calls)
        """

    async def _setup(self) -> None:
        """
        Called during start() before the agent is marked RUNNING.
        Override to: subscribe to events, open connections, load config.
        """

    async def _teardown(self) -> None:
        """
        Called during stop() before the agent is marked STOPPED.
        Override to: close connections, flush buffers, save state.
        """

    # --------------------------------------------------------
    # Lifecycle — start / stop
    # --------------------------------------------------------

    async def start(self) -> None:
        """
        Start the agent:
        1. Mark as STARTING
        2. Register in AgentRegistry
        3. Call _setup() (subscribe to events, etc.)
        4. Start heartbeat loop
        5. Mark as RUNNING

        Raises:
            RuntimeError if the agent is already running
        """
        if self._state == AgentState.RUNNING:
            raise RuntimeError(f"Agent '{self.name}' is already running")

        self._state = AgentState.STARTING
        self._started_at = datetime.utcnow()
        self.logger.info(f"Agent '{self.name}' starting (id={self.agent_id})")

        try:
            # Register in the registry
            if self._registry:
                from core.agent_registery import AgentStatus
                self._registry.register(self.name, self)
                self._registry.update_status(self.name, AgentStatus.RUNNING)

            # Run subclass setup
            await self._setup()

            # Start heartbeat
            if self._registry and self._heartbeat_interval > 0:
                self._heartbeat_task = asyncio.create_task(
                    self._heartbeat_loop(),
                    name=f"{self.name}_heartbeat",
                )

            self._state = AgentState.RUNNING
            self.logger.info(f"Agent '{self.name}' is RUNNING ✅")

        except Exception as e:
            self._state = AgentState.ERROR
            self._error = e
            self.logger.error(f"Agent '{self.name}' failed to start: {e}", exc_info=True)

            if self._registry:
                from core.agent_registery import AgentStatus
                self._registry.update_status(self.name, AgentStatus.ERROR)

            raise

    async def stop(self) -> None:
        """
        Stop the agent:
        1. Mark as STOPPING
        2. Cancel heartbeat
        3. Call _teardown()
        4. Unregister from AgentRegistry
        5. Mark as STOPPED
        """
        if self._state not in (AgentState.RUNNING, AgentState.ERROR):
            self.logger.warning(f"Agent '{self.name}' is not running — skipping stop")
            return

        self._state = AgentState.STOPPING
        self.logger.info(f"Agent '{self.name}' stopping...")

        # Cancel heartbeat
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass

        # Run subclass teardown
        try:
            await self._teardown()
        except Exception as e:
            self.logger.error(f"Error during teardown of '{self.name}': {e}", exc_info=True)

        # Unsubscribe from all events on the bus
        self.unsubscribe_all()

        # Unregister from registry
        if self._registry:
            from core.agent_registery import AgentStatus
            self._registry.update_status(self.name, AgentStatus.STOPPED)
            self._registry.unregister(self.name)

        self._state = AgentState.STOPPED
        self.logger.info(f"Agent '{self.name}' STOPPED 🛑")

    # --------------------------------------------------------
    # Event subscription helpers
    # --------------------------------------------------------

    def subscribe(self, event_type: "EventType", handler: Callable) -> None:
        """
        Subscribe to an event type on the EventBus.
        Wraps bus.subscribe() and tracks subscriptions for cleanup.

        Example:
            self.subscribe(EventType.INCIDENT_CREATED, self.handle_event)
        """
        if not self._bus:
            raise RuntimeError(f"Agent '{self.name}' has no EventBus — pass it in __init__")

        self._bus.subscribe(event_type, handler)
        self._subscriptions.append((event_type, handler))
        self.logger.debug(f"Subscribed to {event_type}")

    def unsubscribe_all(self) -> None:
        """Unsubscribe from all events. Called automatically during stop()."""
        if not self._bus:
            return
        for event_type, handler in self._subscriptions:
            try:
                self._bus.unsubscribe(event_type, handler)
            except Exception:
                pass
        self._subscriptions.clear()

    async def publish(self, event: "Event") -> None:
        """
        Publish an event on the EventBus.

        Example:
            await self.publish(Event(
                type=EventType.REMEDIATION_COMPLETE,
                source=self.name,
                incident_id=incident_id,
                data={"success": True}
            ))
        """
        if not self._bus:
            raise RuntimeError(f"Agent '{self.name}' has no EventBus")
        await self._bus.publish(event)

    # --------------------------------------------------------
    # Heartbeat
    # --------------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        """
        Periodically signals to the registry that this agent is alive.
        Runs as a background task while the agent is RUNNING.
        """
        while self._state == AgentState.RUNNING:
            try:
                await asyncio.sleep(self._heartbeat_interval)
                if self._registry:
                    self._registry.heartbeat(self.name)
                    self.logger.debug(f"Heartbeat: {self.name}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.warning(f"Heartbeat error: {e}")

    # --------------------------------------------------------
    # Properties
    # --------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._state == AgentState.RUNNING

    @property
    def state(self) -> AgentState:
        return self._state

    @property
    def uptime(self) -> Optional[float]:
        """Seconds since agent started. None if not started."""
        if self._started_at:
            return (datetime.utcnow() - self._started_at).total_seconds()
        return None

    @property
    def last_error(self) -> Optional[Exception]:
        return self._error

    # --------------------------------------------------------
    # Info
    # --------------------------------------------------------

    def get_info(self) -> dict:
        """Return a summary of the agent's current state."""
        return {
            "name":       self.name,
            "agent_id":   self.agent_id,
            "state":      self._state.value,
            "uptime_sec": round(self.uptime, 1) if self.uptime else None,
            "started_at": self._started_at.isoformat() if self._started_at else None,
            "error":      str(self._error) if self._error else None,
        }

    def __repr__(self):
        return (
            f"{self.__class__.__name__}("
            f"name={self.name}, "
            f"state={self._state.value})")