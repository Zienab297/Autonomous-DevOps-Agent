"""repositories/alert_repo.py — Alert persistence."""

import logging
from datetime import datetime, timezone
from typing   import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from models.alert import AlertModel

logger = logging.getLogger(__name__)


class AlertRepository:

    def __init__(self, session: Session):
        self._s = session

    def create(self, alert_obj, project_id: str, incident_id: str) -> AlertModel:
        severity = getattr(alert_obj, "severity", None)
        if hasattr(severity, "value"):
            severity = severity.value
        now = datetime.now(timezone.utc)
        row = AlertModel(
            project_id  = project_id,
            incident_id = incident_id,
            title       = getattr(alert_obj, "title",    None),
            severity    = str(severity) if severity else None,
            channel     = getattr(alert_obj, "channel",  None),
            sent        = bool(getattr(alert_obj, "sent", False)),
            created_at  = getattr(alert_obj, "created_at", None) or now,
            sent_at     = getattr(alert_obj, "sent_at",    None),
        )
        self._s.add(row)
        self._s.flush()
        logger.info(
            "[DB][alert_repo] created id=%s incident_id=%s channel=%s",
            row.id, incident_id, row.channel,
        )
        return row

    def get_by_id(self, alert_id: int) -> Optional[AlertModel]:
        return self._s.get(AlertModel, alert_id)

    def list_by_incident(self, project_id: str, incident_id: str) -> List[AlertModel]:
        stmt = (
            select(AlertModel)
            .where(
                AlertModel.project_id  == project_id,
                AlertModel.incident_id == incident_id,
            )
            .order_by(AlertModel.created_at.desc())
        )
        return list(self._s.scalars(stmt).all())

    def list_recent(self, project_id: str, limit: int = 20) -> List[AlertModel]:
        stmt = (
            select(AlertModel)
            .where(AlertModel.project_id == project_id)
            .order_by(AlertModel.created_at.desc())
            .limit(limit)
        )
        return list(self._s.scalars(stmt).all())