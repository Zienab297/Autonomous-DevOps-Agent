"""
Core Package
=============
The foundational layer of the DevOps Agent SDK.

Import everything you need from here:

    from core import EventBus, EventType, Event
    from core import StateManager, IncidentStatus, IncidentSeverity
    from core import ConfigLoader, AppConfig
    from core import Orchestrator
"""

from .event_bus import Event, EventBus, EventType
from .models import (
    Alert,
    DeploymentRecord,
    Incident,
    IncidentSeverity,
    IncidentStatus,
    RemediationAction,
    RemediationResult,
    Solution,
)
from .agent_registery import AgentRegistry, AgentStatus, AgentRecord
from .context_manager import IncidentContext 
from .state_manager import StateManager
from .orchestrator import Orchestrator

__all__ = [
    # EventBus
    "Event",
    "EventBus",
    "EventType",
    # Models
    "Alert",
    "DeploymentRecord",
    "Incident",
    "IncidentSeverity",
    "IncidentStatus",
    "RemediationAction",
    "RemediationResult",
    "Solution",
    # Context
    "IncidentContext",
    # StateManager
    "StateManager",
    # AgentRegistry
    "AgentRegistry",
    "AgentRecord",
    "AgentStatus",
    # Orchestrator
    "Orchestrator",
]