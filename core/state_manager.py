from typing import Optional
from models import (
    Incident, IncidentStatus,
    FixRecord,
    DeploymentRecord
)


class StateManager:

    def __init__(self):
        self._incidents = {}
        self._fix_records = {}
        self._deployments = {}

    # --- Incidents ---

    def create_incident(self, incident: Incident) -> Incident:
        self._incidents[incident.id] = incident
        return incident

    def get_incident(self, incident_id: str) -> Optional[Incident]:
        return self._incidents.get(incident_id)

    def update_incident_status(self, incident_id: str, status: IncidentStatus):
        incident = self._incidents.get(incident_id)
        if incident:
            incident.status = status

    def get_incidents_by_service(self, service: str) -> list[Incident]:
        return [i for i in self._incidents.values() if i.service == service]

    def get_active_incidents(self) -> list[Incident]:
        closed = {IncidentStatus.RESOLVED, IncidentStatus.FAILED}
        return [i for i in self._incidents.values() if i.status not in closed]

    # --- Fix Records ---

    def save_fix_record(self, fix_record: FixRecord) -> FixRecord:
        self._fix_records[fix_record.id] = fix_record
        return fix_record

    def get_fix_records_for_incident(self, incident_id: str) -> list[FixRecord]:
        return [f for f in self._fix_records.values() if f.incident_id == incident_id]

    def update_fix_success(self, fix_id: str, success: bool):
        fix = self._fix_records.get(fix_id)
        if fix:
            fix.success = success

    # --- Deployments ---

    def save_deployment(self, deployment: DeploymentRecord) -> DeploymentRecord:
        self._deployments[deployment.id] = deployment
        return deployment

    def get_recent_deployments(self, service: str, limit: int = 3) -> list[DeploymentRecord]:
        service_deployments = [
            d for d in self._deployments.values() if d.service == service
        ]
        sorted_deployments = sorted(
            service_deployments, key=lambda d: d.deployed_at, reverse=True
        )
        return sorted_deployments[:limit]

    def update_deployment_status(self, deployment_id: str, status: str):
        deployment = self._deployments.get(deployment_id)
        if deployment:
            deployment.status = status
