"""
AgentRegistry - Agent Registration & Discovery
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional, TYPE_CHECKING

logger = logging.getLogger(__name__)


# ============================================================
# AgentStatus
# ============================================================

class AgentStatus(str, Enum):
    IDLE    = "idle"      # Registered but not started
    RUNNING = "running"   # Active and listening for Events
    STOPPED = "stopped"   # Gracefully shut down
    ERROR   = "error"     # Failed to start or crashed


# ============================================================
# AgentRecord - Metadata stored per Agent
# ============================================================

@dataclass
class AgentRecord:
    """
    Metadata about a registered Agent.
    Stored in the AgentRegistry when an Agent calls register().

    Example:
        AgentRecord(
            name="monitoring_agent",
            agent_id="monitoring_agent-a1b2c3",
            status=AgentStatus.RUNNING
        )
    """
    name: str
    agent_id: str
    status: AgentStatus = AgentStatus.IDLE
    registered_at: datetime = field(default_factory=datetime.utcnow)
    last_seen_at: datetime = field(default_factory=datetime.utcnow)

    def __str__(self):
        return (
            f"AgentRecord(name={self.name}, "
            f"id={self.agent_id}, "
            f"status={self.status})"
        )


# ============================================================
# AgentRegistry
# ============================================================

class AgentRegistry:
    """
    Central directory of all Agents in the system.

    Used by the Orchestrator to discover and verify Agents
    before routing Events to them.

    Example:
        registry = AgentRegistry()

        # Agent registers itself on startup
        registry.register("monitoring_agent", "monitoring_agent-a1b2c3")
        registry.set_status("monitoring_agent", AgentStatus.RUNNING)

        # Orchestrator checks before routing
        if registry.is_running("monitoring_agent"):
            await bus.publish(event)

        # List all active agents
        registry.get_all_running()
    """

    def __init__(self):
        # agent_name -> AgentRecord
        self._agents: Dict[str, AgentRecord] = {}
        logger.info("AgentRegistry initialized")

    # --------------------------------------------------------
    # Register / Unregister
    # --------------------------------------------------------

    def register(self, name: str, agent_id: str) -> AgentRecord:
        """
        Register a new Agent in the directory.
        Called by BaseAgent during start().

        Args:
            name:     The Agent's name (e.g. "monitoring_agent")
            agent_id: The unique Agent ID (e.g. "monitoring_agent-a1b2c3")

        Returns:
            The created AgentRecord
        """
        record = AgentRecord(name=name, agent_id=agent_id)
        self._agents[name] = record
        logger.info(f"Agent registered: {record}")
        return record

    def unregister(self, name: str) -> None:
        """
        Remove an Agent from the directory.
        Called by BaseAgent during stop().
        """
        if name in self._agents:
            del self._agents[name]
            logger.info(f"Agent unregistered: {name}")
        else:
            logger.warning(f"Tried to unregister unknown agent: {name}")

    # --------------------------------------------------------
    # Status Management
    # --------------------------------------------------------

    def set_status(self, name: str, status: AgentStatus) -> None:
        """
        Update the status of a registered Agent.

        Called by BaseAgent when:
            - start()  -> RUNNING
            - stop()   -> STOPPED
            - error    -> ERROR
        """
        record = self._agents.get(name)
        if not record:
            logger.warning(f"Cannot set status — agent not found: {name}")
            return

        record.status = status
        record.last_seen_at = datetime.utcnow()
        logger.debug(f"Agent status updated: {name} -> {status}")

    def heartbeat(self, name: str) -> None:
        """
        Update the last_seen_at timestamp for an Agent.
        Called periodically by running Agents to signal they are alive.
        """
        record = self._agents.get(name)
        if record:
            record.last_seen_at = datetime.utcnow()

    # --------------------------------------------------------
    # Discovery
    # --------------------------------------------------------

    def get(self, name: str) -> Optional[AgentRecord]:
        """
        Get a specific Agent's record by name.
        Returns None if not found.
        """
        return self._agents.get(name)

    def is_registered(self, name: str) -> bool:
        """Check if an Agent is registered (regardless of status)."""
        return name in self._agents

    def is_running(self, name: str) -> bool:
        """
        Check if a specific Agent is currently running.
        Used by Orchestrator before routing Events.

        Example:
            if not registry.is_running("knowledge_agent"):
                logger.error("KnowledgeAgent is not available!")
        """
        record = self._agents.get(name)
        return record is not None and record.status == AgentStatus.RUNNING

    def get_all(self) -> List[AgentRecord]:
        """Return all registered Agents (any status)."""
        return list(self._agents.values())

    def get_all_running(self) -> List[AgentRecord]:
        """Return only Agents with RUNNING status."""
        return [
            record for record in self._agents.values()
            if record.status == AgentStatus.RUNNING
        ]

    def get_all_by_status(self, status: AgentStatus) -> List[AgentRecord]:
        """Return all Agents with a specific status."""
        return [
            record for record in self._agents.values()
            if record.status == status
        ]

    # --------------------------------------------------------
    # Validation — used by Orchestrator on startup
    # --------------------------------------------------------

    def verify_required_agents(self, required: List[str]) -> List[str]:
        """
        Check that all required Agents are registered and running.
        Returns a list of missing Agent names (empty = all good).

        Example:
            missing = registry.verify_required_agents([
                "monitoring_agent",
                "knowledge_agent",
                "self_healing_agent",
                "alerting_agent",
            ])
            if missing:
                raise RuntimeError(f"Missing agents: {missing}")
        """
        missing = []
        for name in required:
            if not self.is_running(name):
                missing.append(name)
                logger.warning(f"Required agent not running: {name}")
        return missing

    # --------------------------------------------------------
    # Stats
    # --------------------------------------------------------

    def get_stats(self) -> dict:
        """Return a summary of Agent statuses."""
        stats = {status.value: 0 for status in AgentStatus}
        for record in self._agents.values():
            stats[record.status.value] += 1

        return {
            "total": len(self._agents),
            "by_status": stats,
        }

    def __repr__(self):
        return (
            f"AgentRegistry("
            f"total={len(self._agents)}, "
            f"running={len(self.get_all_running())})"
        )
