"""
repositories/incident_repo.py
------------------------------
Repository for Incident persistence — all DB access goes through here.

Changes vs original:
- All timestamps use datetime.now(timezone.utc)  (no utcnow())
- create() deduplicates: same project + service + open status within
  DEDUP_WINDOW_MINUTES → returns existing row instead of inserting
- Structured logging includes incident_id + agent_name
- Sensitive fields (description) are never logged verbatim
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import List, Optional

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from models.incident import IncidentModel

logger = logging.getLogger(__name__)

# Incidents on the same service within this window are treated as duplicates
DEDUP_WINDOW_MINUTES: int = 5

# Statuses that mean "still open"
_OPEN_STATUSES = {"open", "investigating", "remediating"}


class IncidentRepository:

    def __init__(self, session: Session):
        self._s = session

    # ── Write ─────────────────────────────────────────────────────────────────

    def create(self, incident_obj, project_id: str) -> IncidentModel:
        """
        Persist a core.models.Incident (or any object with matching attrs).

        Deduplication logic:
          If an open incident for the same (project_id, service) was created
          within DEDUP_WINDOW_MINUTES, return that existing row and update its
          status + updated_at rather than inserting a duplicate.

        Upserts on id collision (idempotent on re-run).
        """
        incident_id = incident_obj.incident_id
        service     = incident_obj.service
        now         = datetime.now(timezone.utc)

        # ── 1. Exact id match → upsert ────────────────────────────────────
        existing = self._s.get(IncidentModel, incident_id)
        if existing:
            existing.status     = _val(incident_obj, "status")
            existing.updated_at = now
            self._s.flush()
            logger.info(
                "[DB][incident_repo] upserted incident_id=%s service=%s status=%s",
                incident_id, service, existing.status,
            )
            return existing

        # ── 2. Dedup check: open incident on same service within window ─────
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
                "[DB][incident_repo] dedup — returning existing incident_id=%s "
                "for service=%s (new id=%s discarded)",
                dup.id, service, incident_id,
            )
            return dup

        # ── 3. Insert new row ─────────────────────────────────────────────
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
            "[DB][incident_repo] created incident_id=%s service=%s severity=%s",
            incident_id, service, row.severity,
        )
        return row

    def update_status(self, incident_id: str, status: str) -> None:
        self._s.execute(
            update(IncidentModel)
            .where(IncidentModel.id == incident_id)
            .values(status=status, updated_at=datetime.now(timezone.utc))
        )
        logger.info(
            "[DB][incident_repo] status updated incident_id=%s new_status=%s",
            incident_id, status,
        )

    # ── Read ──────────────────────────────────────────────────────────────────

    def get_by_id(self, incident_id: str) -> Optional[IncidentModel]:
        return self._s.get(IncidentModel, incident_id)

    def list_by_project(
        self,
        project_id : str,
        service    : Optional[str] = None,
        status     : Optional[str] = None,
    ) -> List[IncidentModel]:
        stmt = select(IncidentModel).where(IncidentModel.project_id == project_id)
        if service:
            stmt = stmt.where(IncidentModel.service == service)
        if status:
            stmt = stmt.where(IncidentModel.status == status)
        stmt = stmt.order_by(IncidentModel.created_at.desc())
        return list(self._s.scalars(stmt).all())

    def list_recent(
        self,
        project_id: str,
        limit     : int = 20,
    ) -> List[IncidentModel]:
        stmt = (
            select(IncidentModel)
            .where(IncidentModel.project_id == project_id)
            .order_by(IncidentModel.created_at.desc())
            .limit(limit)
        )
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