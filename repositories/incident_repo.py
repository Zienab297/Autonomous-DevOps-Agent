"""repositories/incident_repo.py — Incident persistence with deduplication."""

import logging
from datetime import datetime, timezone, timedelta
from typing   import List, Optional

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from models.incident import IncidentModel

logger = logging.getLogger(__name__)

DEDUP_WINDOW_MINUTES = 5
_OPEN_STATUSES = {"open", "investigating", "remediating"}


class IncidentRepository:

    def __init__(self, session: Session):
        self._s = session

    def create(self, incident_obj, project_id: str) -> IncidentModel:
        """
        Persist an incident.  Deduplicates: if an open incident exists for
        the same (project_id, service) within DEDUP_WINDOW_MINUTES, returns
        the existing row instead of creating a duplicate.
        """
        incident_id = incident_obj.incident_id
        service     = incident_obj.service
        now         = datetime.now(timezone.utc)

        # 1. Exact id match → upsert
        existing = self._s.get(IncidentModel, incident_id)
        if existing:
            existing.status     = _val(incident_obj, "status")
            existing.updated_at = now
            self._s.flush()
            logger.info("[DB][incident_repo] upserted id=%s", incident_id)
            return existing

        # 2. Dedup within window
        window_start = now - timedelta(minutes=DEDUP_WINDOW_MINUTES)
        stmt = (
            select(IncidentModel)
            .where(
                IncidentModel.project_id == project_id,
                IncidentModel.service    == service,
                IncidentModel.status.in_(_OPEN_STATUSES),
                IncidentModel.created_at >= window_start,
            )
            .order_by(IncidentModel.created_at.desc())
            .limit(1)
        )
        dup = self._s.scalars(stmt).first()
        if dup:
            dup.updated_at = now
            self._s.flush()
            logger.info(
                "[DB][incident_repo] dedup — reusing id=%s for service=%s",
                dup.id, service,
            )
            return dup

        # 3. Insert
        row = IncidentModel(
            id          = incident_id,
            project_id  = project_id,
            service     = service,
            severity    = _val(incident_obj, "severity"),
            status      = _val(incident_obj, "status"),
            description = getattr(incident_obj, "description", None),
            created_at  = getattr(incident_obj, "created_at", None) or now,
            updated_at  = getattr(incident_obj, "updated_at", None) or now,
        )
        self._s.add(row)
        self._s.flush()
        logger.info(
            "[DB][incident_repo] created id=%s service=%s severity=%s",
            incident_id, service, row.severity,
        )
        return row

    def update_status(self, incident_id: str, status: str) -> None:
        self._s.execute(
            update(IncidentModel)
            .where(IncidentModel.id == incident_id)
            .values(status=status, updated_at=datetime.now(timezone.utc))
        )
        logger.info("[DB][incident_repo] status=%s id=%s", status, incident_id)

    def get_by_id(self, incident_id: str) -> Optional[IncidentModel]:
        return self._s.get(IncidentModel, incident_id)

    def list_by_project(
        self,
        project_id: str,
        service   : Optional[str] = None,
        status    : Optional[str] = None,
        limit     : int = 100,
    ) -> List[IncidentModel]:
        stmt = select(IncidentModel).where(IncidentModel.project_id == project_id)
        if service:
            stmt = stmt.where(IncidentModel.service == service)
        if status:
            stmt = stmt.where(IncidentModel.status == status)
        stmt = stmt.order_by(IncidentModel.created_at.desc()).limit(limit)
        return list(self._s.scalars(stmt).all())

    def list_active(self, project_id: str) -> List[IncidentModel]:
        stmt = (
            select(IncidentModel)
            .where(
                IncidentModel.project_id == project_id,
                IncidentModel.status.in_(_OPEN_STATUSES),
            )
            .order_by(IncidentModel.created_at.desc())
        )
        return list(self._s.scalars(stmt).all())


def _val(obj, attr: str) -> str:
    v = getattr(obj, attr, "unknown")
    return v.value if hasattr(v, "value") else str(v)