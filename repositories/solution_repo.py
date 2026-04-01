"""repositories/solution_repo.py — Solution persistence."""

import logging
from datetime import datetime, timezone
from typing   import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from models.solution import SolutionModel

logger = logging.getLogger(__name__)


class SolutionRepository:

    def __init__(self, session: Session):
        self._s = session

    def create(self, solution_obj, project_id: str, incident_id: str) -> SolutionModel:
        source = getattr(solution_obj, "source", None)
        if hasattr(source, "value"):
            source = source.value
        now = datetime.now(timezone.utc)
        row = SolutionModel(
            project_id  = project_id,
            incident_id = incident_id,
            source      = str(source) if source else "unknown",
            confidence  = getattr(solution_obj, "confidence", None),
            content     = getattr(solution_obj, "healing_prompt", None),
            created_at  = getattr(solution_obj, "created_at", None) or now,
        )
        self._s.add(row)
        self._s.flush()
        logger.info(
            "[DB][solution_repo] created id=%s incident_id=%s source=%s",
            row.id, incident_id, row.source,
        )
        return row

    def get_by_id(self, solution_id: int) -> Optional[SolutionModel]:
        return self._s.get(SolutionModel, solution_id)

    def list_by_incident(self, project_id: str, incident_id: str) -> List[SolutionModel]:
        stmt = (
            select(SolutionModel)
            .where(
                SolutionModel.project_id  == project_id,
                SolutionModel.incident_id == incident_id,
            )
            .order_by(SolutionModel.confidence.desc().nullslast())
        )
        return list(self._s.scalars(stmt).all())

    def list_recent(self, project_id: str, limit: int = 20) -> List[SolutionModel]:
        stmt = (
            select(SolutionModel)
            .where(SolutionModel.project_id == project_id)
            .order_by(SolutionModel.created_at.desc())
            .limit(limit)
        )
        return list(self._s.scalars(stmt).all())