"""
core/state_manager.py
----------------------
Tracks the state of Incidents and Agents across the system.
The single source of truth for what is happening right now.
"""
from dataclasses import dataclass, field
import logging
from datetime import datetime
from typing import Dict, List, Optional
from core.models import (
    AgentStatus,
    Incident,
    IncidentStatus,
    RemediationAction,
    Solution,
)

logger = logging.getLogger(__name__)


class StateManager:
    """
    The StateManager is the single source of truth for the system.

    It tracks:
        - Active and resolved Incidents
        - Agent statuses
        - Solutions generated per Incident
        - Remediation actions taken per Incident

    Example:
        state = StateManager()

        # Track a new incident
        state.add_incident(incident)

        # Update its status
        state.update_incident_status("INC-001", IncidentStatus.INVESTIGATING)

        # Check what's active
        state.get_active_incidents()
    """

    def __init__(self):
        # incident_id → Incident
        self._incidents: Dict[str, Incident] = {}

        # agent_name → AgentStatus
        self._agent_statuses: Dict[str, AgentStatus] = {}

        # incident_id → list of Solutions
        self._solutions: Dict[str, List[Solution]] = {}

        # incident_id → list of RemediationActions
        self._actions: Dict[str, List[RemediationAction]] = {}

        logger.info("StateManager initialized")

    # ============================================================
    # Incident Management
    # ============================================================

    def add_incident(self, incident: Incident) -> None:
        """Add a new Incident to the state."""
        self._incidents[incident.incident_id] = incident
        logger.info(f"[StateManager] Incident added: {incident}")

    def get_incident(self, incident_id: str) -> Optional[Incident]:
        """Get an Incident by ID."""
        return self._incidents.get(incident_id)

    def update_incident_status(
        self, incident_id: str, status: IncidentStatus
    ) -> None:
        """Update the status of an Incident."""
        incident = self._incidents.get(incident_id)
        if not incident:
            logger.warning(f"[StateManager] Incident not found: {incident_id}")
            return

        incident.status = status
        incident.updated_at = datetime.utcnow()
        logger.info(f"[StateManager] Incident {incident_id} → {status.value}")

    def get_active_incidents(self) -> List[Incident]:
        """Return all Incidents that are not resolved or failed."""
        return [
            i for i in self._incidents.values()
            if i.status not in (IncidentStatus.RESOLVED, IncidentStatus.FAILED)
        ]

    def get_all_incidents(self) -> List[Incident]:
        """Return all Incidents."""
        return list(self._incidents.values())

    def get_resolved_incidents(self) -> List[Incident]:
        """Return all resolved Incidents."""
        return [
            i for i in self._incidents.values()
            if i.status == IncidentStatus.RESOLVED
        ]

    # ============================================================
    # Agent Status Management
    # ============================================================

    def set_agent_status(self, agent_name: str, status: AgentStatus) -> None:
        """Update the status of an Agent."""
        self._agent_statuses[agent_name] = status
        logger.info(f"[StateManager] Agent '{agent_name}' → {status.value}")

    def get_agent_status(self, agent_name: str) -> Optional[AgentStatus]:
        """Get the current status of an Agent."""
        return self._agent_statuses.get(agent_name)

    def get_all_agent_statuses(self) -> Dict[str, AgentStatus]:
        """Return all agent statuses."""
        return dict(self._agent_statuses)

    # ============================================================
    # Solution Management
    # ============================================================

    def add_solution(self, solution: Solution) -> None:
        """Store a Solution generated for an Incident."""
        if solution.incident_id not in self._solutions:
            self._solutions[solution.incident_id] = []
        self._solutions[solution.incident_id].append(solution)
        logger.info(f"[StateManager] Solution added for {solution.incident_id}")

    def get_solutions(self, incident_id: str) -> List[Solution]:
        """Get all Solutions generated for an Incident."""
        return self._solutions.get(incident_id, [])

    def get_best_solution(self, incident_id: str) -> Optional[Solution]:
        """Get the highest confidence Solution for an Incident."""
        solutions = self._solutions.get(incident_id, [])
        if not solutions:
            return None
        return max(solutions, key=lambda s: s.confidence)

    # ============================================================
    # Remediation Action Management
    # ============================================================

    def add_action(self, action: RemediationAction) -> None:
        """Store a RemediationAction taken for an Incident."""
        if action.incident_id not in self._actions:
            self._actions[action.incident_id] = []
        self._actions[action.incident_id].append(action)
        logger.info(f"[StateManager] Action added for {action.incident_id}: {action.command[:50]}")

    def get_actions(self, incident_id: str) -> List[RemediationAction]:
        """Get all RemediationActions taken for an Incident."""
        return self._actions.get(incident_id, [])

    # ============================================================
    # Summary
    # ============================================================

    def summary(self) -> Dict:
        """Return a summary of the current system state."""
        return {
            "total_incidents"  : len(self._incidents),
            "active_incidents" : len(self.get_active_incidents()),
            "resolved_incidents": len(self.get_resolved_incidents()),
            "agents"           : self.get_all_agent_statuses(),
        }

    def __repr__(self):
        return (
            f"StateManager("
            f"incidents={len(self._incidents)}, "
            f"agents={len(self._agent_statuses)})"
        )