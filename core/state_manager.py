"""
StateManager - Incident State Store
=====================================
The central store for all incidents in the system.

Responsibilities:
    - Create new incidents (called by MonitoringAgent)
    - Update incident status as the workflow progresses
    - Resolve or escalate incidents
    - Query incidents by status, service, or ID

Every Agent interacts with StateManager to read and write
the current state of an incident.

Uses the models from models.py — no duplicate data classes here.
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from .models import Incident, IncidentSeverity, IncidentStatus

logger = logging.getLogger(__name__)


class StateManager:
    """
    Stores and manages all Incident objects in the system.

    Example:
        state = StateManager()

        # MonitoringAgent creates an incident
        incident = state.create_incident(
            service="auth-api",
            severity=IncidentSeverity.HIGH,
            description="Error rate spiked to 45%",
            metrics={"error_rate": 0.45},
            logs=["ERROR: connection timeout"]
        )

        # Orchestrator updates the status
        state.update_status(incident.incident_id, IncidentStatus.INVESTIGATING)

        # Orchestrator resolves after fix
        state.resolve_incident(incident.incident_id)
    """

    def __init__(self):
        # incident_id -> Incident
        self._incidents: Dict[str, Incident] = {}
        logger.info("StateManager initialized")

    # --------------------------------------------------------
    # Create
    # --------------------------------------------------------

    def create_incident(
        self,
        service: str,
        severity: IncidentSeverity,
        description: str,
        metrics: Optional[Dict[str, Any]] = None,
        logs: Optional[List[str]] = None,
    ) -> Incident:
        """
        Create a new Incident and store it.
        Called by: MonitoringAgent when an anomaly is detected.

        Returns:
            The created Incident object (from models.py)
        """
        incident = Incident(
            service=service,
            severity=severity,
            description=description,
            metrics=metrics or {},
            logs=logs or [],
        )

        self._incidents[incident.incident_id] = incident
        logger.info(f"Incident created: {incident}")
        return incident

    # --------------------------------------------------------
    # Read
    # --------------------------------------------------------

    def get_incident(self, incident_id: str) -> Optional[Incident]:
        """
        Retrieve an Incident by ID.
        Returns None if not found.
        """
        incident = self._incidents.get(incident_id)
        if not incident:
            logger.warning(f"Incident not found: {incident_id}")
        return incident

    def get_all(self) -> List[Incident]:
        """Return all incidents regardless of status."""
        return list(self._incidents.values())

    def get_active_incidents(self) -> List[Incident]:
        """
        Return all incidents still in progress.
        (DETECTED, INVESTIGATING, REMEDIATING)
        """
        active = {
            IncidentStatus.DETECTED,
            IncidentStatus.INVESTIGATING,
            IncidentStatus.REMEDIATING,
        }
        return [i for i in self._incidents.values() if i.status in active]

    def get_by_service(self, service: str) -> List[Incident]:
        """Return all incidents for a specific service."""
        return [i for i in self._incidents.values() if i.service == service]

    def get_by_status(self, status: IncidentStatus) -> List[Incident]:
        """Return all incidents with a specific status."""
        return [i for i in self._incidents.values() if i.status == status]

    # --------------------------------------------------------
    # Update
    # --------------------------------------------------------

    def update_status(
        self,
        incident_id: str,
        status: IncidentStatus
    ) -> Optional[Incident]:
        """
        Update the status of an incident.
        Called by: Orchestrator at every step of the workflow.

        Example:
            state.update_status(incident_id, IncidentStatus.INVESTIGATING)
            state.update_status(incident_id, IncidentStatus.REMEDIATING)
        """
        incident = self.get_incident(incident_id)
        if not incident:
            return None

        previous = incident.status
        incident.status = status
        incident.updated_at = datetime.utcnow()

        logger.info(f"Status updated: {incident_id} | {previous} -> {status}")
        return incident

    def append_logs(self, incident_id: str, logs: List[str]) -> Optional[Incident]:
        """
        Add new log lines to an existing incident.
        Called by: MonitoringAgent if more logs come in.
        """
        incident = self.get_incident(incident_id)
        if not incident:
            return None

        incident.logs.extend(logs)
        incident.updated_at = datetime.utcnow()
        return incident

    def update_metrics(self, incident_id: str, metrics: Dict[str, Any]) -> Optional[Incident]:
        """
        Merge new metrics into an existing incident.
        Called by: MonitoringAgent during verification.
        """
        incident = self.get_incident(incident_id)
        if not incident:
            return None

        incident.metrics.update(metrics)
        incident.updated_at = datetime.utcnow()
        return incident

    # --------------------------------------------------------
    # Resolve / Fail / Escalate
    # --------------------------------------------------------

    def resolve_incident(self, incident_id: str) -> Optional[Incident]:
        """
        Mark an incident as resolved.
        Called by: Orchestrator after REMEDIATION_COMPLETE.
        """
        incident = self.update_status(incident_id, IncidentStatus.RESOLVED)
        if incident:
            logger.info(f"Incident resolved: {incident_id} ✅")
        return incident

    def fail_incident(self, incident_id: str) -> Optional[Incident]:
        """
        Mark an incident as failed.
        Called by: Orchestrator when all retries are exhausted.
        """
        incident = self.update_status(incident_id, IncidentStatus.FAILED)
        if incident:
            logger.warning(f"Incident failed: {incident_id} ❌")
        return incident

    def escalate_incident(self, incident_id: str) -> Optional[Incident]:
        """
        Mark an incident as escalated for human intervention.
        Called by: Orchestrator when auto-remediation is not possible.
        """
        incident = self.update_status(incident_id, IncidentStatus.ESCALATED)
        if incident:
            logger.warning(f"Incident escalated: {incident_id} ⚠️")
        return incident

    # --------------------------------------------------------
    # Stats
    # --------------------------------------------------------

    def get_stats(self) -> Dict[str, Any]:
        """Return a quick summary of all incidents in the system."""
        by_status: Dict[str, int] = {}
        for status in IncidentStatus:
            count = len(self.get_by_status(status))
            if count > 0:
                by_status[status.value] = count

        return {
            "total":   len(self._incidents),
            "active":  len(self.get_active_incidents()),
            "by_status": by_status,
        }

    def __repr__(self):
        return (
            f"StateManager("
            f"total={len(self._incidents)}, "
            f"active={len(self.get_active_incidents())})"
        )