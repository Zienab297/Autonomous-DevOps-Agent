"""
core/__init__.py
----------------
Exports all core components for easy importing.

Usage:
    from core import Orchestrator
    from core import EventBus, EventType, Event
    from core import StateManager
    from core import ContextManager
    from core import AgentRegistry
    from core.models import Incident, Severity, Solution
"""

from core.event_bus       import EventBus, Event, EventType
from core.state_manager   import StateManager
from core.context_manager import ContextManager, IncidentContext
from core.agent_registery import AgentRegistry, AgentRecord
from core.orchestrator    import Orchestrator
from core.models import (
    # Enums
    Severity,
    IncidentStatus,
    AgentStatus,
    DeploymentStatus,
    RemediationStatus,
    # Models
    Incident,
    Metric,
    Log,
    Solution,
    RemediationAction,
    Alert,
    Deployment,
)

__all__ = [
    # Event System
    "EventBus",
    "Event",
    "EventType",
    # Core Components
    "Orchestrator",
    "StateManager",
    "ContextManager",
    "IncidentContext",
    "AgentRegistry",
    "AgentRecord",
    # Enums
    "Severity",
    "IncidentStatus",
    "AgentStatus",
    "DeploymentStatus",
    "RemediationStatus",
    # Models
    "Incident",
    "Metric",
    "Log",
    "Solution",
    "RemediationAction",
    "Alert",
    "Deployment",
]
