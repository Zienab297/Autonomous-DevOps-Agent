"""repositories/event_repo.py — EventLog persistence (full audit trail)."""

import json
import logging
from datetime import datetime, timezone
from typing   import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from models.event import EventLogModel

logger = logging.getLogger(__name__)


class EventRepository:

    def __init__(self, session: Session):
        self._s = session

    def create(self, event_obj, project_id: str) -> EventLogModel:
        try:
            data_json = json.dumps(getattr(event_obj, "data", {}), default=str)
        except Exception:
            data_json = "{}"

        now = datetime.now(timezone.utc)
        ts  = getattr(event_obj, "timestamp", None) or now
        if hasattr(ts, "tzinfo") and ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)

        row = EventLogModel(
            project_id  = project_id,
            type        = str(getattr(event_obj, "type", "unknown")),
            source      = getattr(event_obj, "source",      None),
            incident_id = getattr(event_obj, "incident_id", None),
            data_json   = data_json,
            created_at  = ts,
        )
        self._s.add(row)
        self._s.flush()
        logger.debug(
            "[DB][event_repo] logged type=%s source=%s incident_id=%s",
            row.type, row.source, row.incident_id,
        )
        return row

    def get_by_id(self, event_id: int) -> Optional[EventLogModel]:
        return self._s.get(EventLogModel, event_id)

    def list_by_project(
        self,
        project_id : str,
        event_type : Optional[str] = None,
        incident_id: Optional[str] = None,
        limit      : int = 100,
    ) -> List[EventLogModel]:
        stmt = select(EventLogModel).where(EventLogModel.project_id == project_id)
        if event_type:
            stmt = stmt.where(EventLogModel.type == event_type)
        if incident_id:
            stmt = stmt.where(EventLogModel.incident_id == incident_id)
        stmt = stmt.order_by(EventLogModel.created_at.desc()).limit(limit)
        return list(self._s.scalars(stmt).all())