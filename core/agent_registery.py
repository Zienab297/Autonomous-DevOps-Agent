"""
core/agent_registry.py
-----------------------
Tracks all registered Agents in the system.
The Orchestrator uses this to know which Agents are available
and how to reach them.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from core.models import AgentStatus

logger = logging.getLogger(__name__)


# ============================================================
# AgentRecord — Info stored about each registered Agent
# ============================================================

@dataclass
class AgentRecord:
    """
    Holds the registration info for a single Agent.

    Example:
        AgentRecord(
            name="monitoring_agent",
            agent=monitoring_agent_instance,
        )
    """
    name: str
    agent: object
    status: AgentStatus = AgentStatus.IDLE
    registered_at: datetime = field(default_factory=datetime.utcnow)
    last_active: Optional[datetime] = None
    metadata: Dict = field(default_factory=dict)

    def __str__(self):
        return (
            f"AgentRecord("
            f"name={self.name}, "
            f"status={self.status.value})"
        )


# ============================================================
# AgentRegistry
# ============================================================

class AgentRegistry:
    """
    The AgentRegistry keeps track of all Agents in the system.

    The Orchestrator uses it to:
        - Register new Agents
        - Look up Agents by name
        - Check Agent statuses
        - Get all available Agents

    Example:
        registry = AgentRegistry()

        # Register an Agent
        registry.register("monitoring_agent", monitoring_agent)

        # Get it back
        agent = registry.get("monitoring_agent")

        # Update its status
        registry.update_status("monitoring_agent", AgentStatus.RUNNING)
    """

    def __init__(self):
        # agent_name → AgentRecord
        self._agents: Dict[str, AgentRecord] = {}
        logger.info("AgentRegistry initialized")

    # ============================================================
    # Registration
    # ============================================================

    def register(
        self,
        name: str,
        agent: object,
        metadata: Optional[Dict] = None,
    ) -> AgentRecord:
        """
        Register a new Agent in the system.

        Args:
            name:     Unique name for the Agent
            agent:    The Agent instance
            metadata: Optional extra info about the Agent

        Returns:
            AgentRecord
        """
        if name in self._agents:
            logger.warning(f"[AgentRegistry] Agent '{name}' already registered — overwriting")

        record = AgentRecord(
            name=name,
            agent=agent,
            metadata=metadata or {},
        )
        self._agents[name] = record
        logger.info(f"[AgentRegistry] Registered: {record}")
        return record

    def unregister(self, name: str) -> None:
        """Remove an Agent from the registry."""
        if name in self._agents:
            del self._agents[name]
            logger.info(f"[AgentRegistry] Unregistered: '{name}'")
        else:
            logger.warning(f"[AgentRegistry] Agent '{name}' not found")

    # ============================================================
    # Lookup
    # ============================================================

    def get(self, name: str) -> Optional[AgentRecord]:
        """Get an AgentRecord by name."""
        return self._agents.get(name)

    def get_agent(self, name: str) -> Optional[object]:
        """Get the Agent instance directly by name."""
        record = self._agents.get(name)
        return record.agent if record else None

    def get_all(self) -> List[AgentRecord]:
        """Return all registered AgentRecords."""
        return list(self._agents.values())

    def get_all_names(self) -> List[str]:
        """Return all registered Agent names."""
        return list(self._agents.keys())

    # ============================================================
    # Status Management
    # ============================================================

    def update_status(self, name: str, status: AgentStatus) -> None:
        """Update the status of a registered Agent."""
        record = self._agents.get(name)
        if not record:
            logger.warning(f"[AgentRegistry] Agent '{name}' not found")
            return

        record.status = status
        record.last_active = datetime.utcnow()
        logger.info(f"[AgentRegistry] '{name}' → {status.value}")

    def get_status(self, name: str) -> Optional[AgentStatus]:
        """Get the current status of an Agent."""
        record = self._agents.get(name)
        return record.status if record else None

    def get_available_agents(self) -> List[AgentRecord]:
        """Return all Agents that are IDLE or RUNNING."""
        return [
            r for r in self._agents.values()
            if r.status in (AgentStatus.IDLE, AgentStatus.RUNNING)
        ]

    def is_registered(self, name: str) -> bool:
        """Check if an Agent is registered."""
        return name in self._agents

    # ============================================================
    # Summary
    # ============================================================

    def summary(self) -> Dict:
        """Return a summary of all registered Agents."""
        return {
            record.name: record.status.value
            for record in self._agents.values()
        }

    def __repr__(self):
        return f"AgentRegistry(agents={list(self._agents.keys())})"