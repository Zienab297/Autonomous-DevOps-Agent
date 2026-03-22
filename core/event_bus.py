"""
EventBus - Core Communication Layer
====================================
Responsible for delivering Events between Agents
instead of Agents communicating directly with each other.
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# ============================================================
# Event Types - All possible Events in the system
# ============================================================

class EventType(str, Enum):
    # Monitoring Events
    ANOMALY_DETECTED    = "monitoring.anomaly_detected"
    INCIDENT_CREATED    = "monitoring.incident_created"
    METRICS_COLLECTED   = "monitoring.metrics_collected"

    # Investigation Events
    INVESTIGATION_STARTED   = "knowledge.investigation_started"
    INVESTIGATION_COMPLETE  = "knowledge.investigation_complete"

    # Remediation Events
    REMEDIATION_STARTED     = "healing.remediation_started"
    REMEDIATION_COMPLETE    = "healing.remediation_complete"
    REMEDIATION_FAILED      = "healing.remediation_failed"

    # CI/CD Events
    DEPLOYMENT_STARTED      = "cicd.deployment_started"
    DEPLOYMENT_COMPLETE     = "cicd.deployment_complete"
    ROLLBACK_TRIGGERED      = "cicd.rollback_triggered"

    # Alerting Events
    ALERT_SENT              = "alerting.alert_sent"
    REPORT_SENT             = "alerting.report_sent"

    # System Events
    AGENT_REGISTERED        = "system.agent_registered"
    AGENT_STOPPED           = "system.agent_stopped"


# ============================================================
# Event Model - The structure of an Event passed between Agents
# ============================================================

@dataclass
class Event:
    """
    An Event is the message that travels between Agents.

    Example:
        Event(
            type=EventType.INCIDENT_CREATED,
            source="monitoring_agent",
            data={"service": "auth-api", "error_rate": 0.45}
        )
    """
    type: EventType
    source: str                                              # Which agent sent this Event
    data: Dict[str, Any] = field(default_factory=dict)      # Payload
    incident_id: Optional[str] = None                       # Linked incident (if any)
    timestamp: datetime = field(default_factory=datetime.utcnow)
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def __str__(self):
        return (
            f"Event(id={self.event_id[:8]}, "
            f"type={self.type}, "
            f"source={self.source})"
        )


# ============================================================
# EventBus - The heart of the system
# ============================================================

class EventBus:
    """
    The EventBus acts as the communication broker between all Agents.

    How it works:
        - An Agent publishes an Event  ->  publish()
        - An Agent listens for Events  ->  subscribe()
        - EventBus delivers the Event to all registered subscribers

    Example:
        bus = EventBus()

        # Agent registers to listen
        bus.subscribe(EventType.INCIDENT_CREATED, my_handler)

        # Another Agent publishes
        await bus.publish(Event(
            type=EventType.INCIDENT_CREATED,
            source="monitoring_agent",
            data={"service": "auth-api"}
        ))
    """

    def __init__(self):
        # Maps each EventType -> list of handlers subscribed to it
        self._subscribers: Dict[EventType, List[Callable]] = {}

        # Full log of all Events that have been published
        self._history: List[Event] = []

        # Async queue for deferred Event delivery
        self._queue: asyncio.Queue = asyncio.Queue()

        self._running = False
        logger.info("EventBus initialized")

    # --------------------------------------------------------
    # Subscribe - Register a handler for a specific Event type
    # --------------------------------------------------------

    def subscribe(self, event_type: EventType, handler: Callable) -> None:
        """
        Register a handler to be called when the given EventType is published.

        Args:
            event_type: The Event type to listen for
            handler:    The function to call (supports both async and sync)

        Example:
            bus.subscribe(EventType.INCIDENT_CREATED, handle_incident)
        """
        if event_type not in self._subscribers:
            self._subscribers[event_type] = []

        self._subscribers[event_type].append(handler)
        logger.debug(f"Subscribed '{handler.__name__}' -> {event_type}")

    def unsubscribe(self, event_type: EventType, handler: Callable) -> None:
        """
        Remove a handler from the subscribers list.

        Args:
            event_type: The Event type to stop listening for
            handler:    The handler to remove
        """
        if event_type in self._subscribers:
            self._subscribers[event_type].remove(handler)
            logger.debug(f"Unsubscribed '{handler.__name__}' <- {event_type}")

    # --------------------------------------------------------
    # Publish - Dispatch an Event to all subscribers
    # --------------------------------------------------------

    async def publish(self, event: Event) -> None:
        """
        Publish an Event and deliver it to all registered subscribers.

        Args:
            event: The Event to dispatch

        Example:
            await bus.publish(Event(
                type=EventType.INCIDENT_CREATED,
                source="monitoring_agent",
                data={"service": "auth-api"}
            ))
        """
        # Save to history before dispatching
        self._history.append(event)
        logger.info(f"Publishing: {event}")

        # Retrieve all handlers subscribed to this Event type
        handlers = self._subscribers.get(event.type, [])

        if not handlers:
            logger.warning(f"No subscribers for {event.type}")
            return

        # Invoke each handler (supports both async and sync)
        for handler in handlers:
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(event)    # async handler
                else:
                    handler(event)          # sync handler

                logger.debug(f"Handler '{handler.__name__}' executed successfully")

            except Exception as e:
                logger.error(
                    f"Handler '{handler.__name__}' raised an error: {e}",
                    exc_info=True
                )

    def publish_sync(self, event: Event) -> None:
        """
        Publish an Event from synchronous code.
        Schedules the publish as an async task.
        """
        asyncio.create_task(self.publish(event))

    # --------------------------------------------------------
    # History & Debugging
    # --------------------------------------------------------

    def get_history(
        self,
        event_type: Optional[EventType] = None,
        incident_id: Optional[str] = None,
        limit: int = 50
    ) -> List[Event]:
        """
        Retrieve the log of published Events.

        Args:
            event_type:  Filter by a specific Event type (optional)
            incident_id: Filter by a specific incident ID (optional)
            limit:       Maximum number of Events to return

        Examples:
            bus.get_history()
            bus.get_history(event_type=EventType.INCIDENT_CREATED)
            bus.get_history(incident_id="INC-001")
        """
        history = self._history

        if event_type:
            history = [e for e in history if e.type == event_type]

        if incident_id:
            history = [e for e in history if e.incident_id == incident_id]

        return history[-limit:]

    def get_subscribers_count(self, event_type: EventType) -> int:
        """Return the number of handlers subscribed to a given Event type."""
        return len(self._subscribers.get(event_type, []))

    def clear_history(self) -> None:
        """Clear the full Event history log."""
        self._history.clear()
        logger.info("EventBus history cleared")

    def __repr__(self):
        total_subs = sum(len(v) for v in self._subscribers.values())
        return (
            f"EventBus("
            f"subscribers={total_subs}, "
            f"history={len(self._history)})"
        )