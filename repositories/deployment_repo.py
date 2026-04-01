"""repositories/deployment_repo.py — Deployment persistence."""

import logging
from datetime import datetime, timezone
from typing   import List, Optional

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from models.deployment import DeploymentModel

logger = logging.getLogger(__name__)


class DeploymentRepository:

    def __init__(self, session: Session):
        self._s = session

    def create_or_update(self, dep_obj, project_id: str) -> DeploymentModel:
        dep_id   = getattr(dep_obj, "deployment_id", None) or getattr(dep_obj, "id", None)
        now      = datetime.now(timezone.utc)

        if dep_id:
            existing = self._s.get(DeploymentModel, dep_id)
            if existing:
                existing.status       = _val(dep_obj, "status")
                existing.conclusion   = getattr(dep_obj, "conclusion", None)
                existing.pipeline_url = getattr(dep_obj, "pipeline_url", None)
                existing.finished_at  = getattr(dep_obj, "finished_at", None)
                self._s.flush()
                return existing

        row = DeploymentModel(
            id           = dep_id or f"dep-{int(now.timestamp())}",
            project_id   = project_id,
            service      = getattr(dep_obj, "service",      "unknown"),
            branch       = getattr(dep_obj, "branch",       "main"),
            version      = getattr(dep_obj, "version",      None),
            status       = _val(dep_obj, "status"),
            conclusion   = getattr(dep_obj, "conclusion",   None),
            pipeline_url = getattr(dep_obj, "pipeline_url", None),
            run_id       = str(getattr(dep_obj, "run_id", "") or ""),
            started_at   = getattr(dep_obj, "started_at",   None),
            finished_at  = getattr(dep_obj, "finished_at",  None),
            created_at   = getattr(dep_obj, "created_at",   None) or now,
        )
        self._s.add(row)
        self._s.flush()
        logger.info(
            "[DB][deployment_repo] created id=%s service=%s status=%s",
            row.id, row.service, row.status,
        )
        return row

    def update_status(
        self,
        deployment_id: str,
        status       : str,
        conclusion   : Optional[str]      = None,
        pipeline_url : Optional[str]      = None,
        finished_at  : Optional[datetime] = None,
    ) -> None:
        values: dict = {"status": status}
        if conclusion   : values["conclusion"]   = conclusion
        if pipeline_url : values["pipeline_url"] = pipeline_url
        if finished_at  : values["finished_at"]  = finished_at
        self._s.execute(
            update(DeploymentModel)
            .where(DeploymentModel.id == deployment_id)
            .values(**values)
        )

    def get_by_id(self, deployment_id: str) -> Optional[DeploymentModel]:
        return self._s.get(DeploymentModel, deployment_id)

    def list_recent(self, project_id: str, limit: int = 20) -> List[DeploymentModel]:
        stmt = (
            select(DeploymentModel)
            .where(DeploymentModel.project_id == project_id)
            .order_by(DeploymentModel.created_at.desc())
            .limit(limit)
        )
        return list(self._s.scalars(stmt).all())


def _val(obj, attr: str) -> str:
    v = getattr(obj, attr, "unknown")
    return v.value if hasattr(v, "value") else str(v)