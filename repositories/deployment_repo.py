"""
repositories/deployment_repo.py
--------------------------------
Repository for Deployment persistence.

Changes vs original:
- All timestamps use datetime.now(timezone.utc)
- Structured logging includes deployment_id + service
"""

import logging
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from models.deployment import DeploymentModel

logger = logging.getLogger(__name__)


class DeploymentRepository:

    def __init__(self, session: Session):
        self._s = session

    def create(self, dep_obj, project_id: str) -> DeploymentModel:
        now = datetime.now(timezone.utc)

        existing = self._s.get(DeploymentModel, dep_obj.deployment_id)
        if existing:
            existing.status       = _val(dep_obj, "status")
            existing.pipeline_url = getattr(dep_obj, "pipeline_url", None)
            existing.finished_at  = getattr(dep_obj, "finished_at", None)
            self._s.flush()
            logger.info(
                "[DB][deployment_repo] upserted deployment_id=%s service=%s status=%s",
                existing.id, existing.service, existing.status,
            )
            return existing

        row = DeploymentModel(
            id           = dep_obj.deployment_id,
            project_id   = project_id,
            service      = dep_obj.service,
            branch       = dep_obj.branch,
            version      = getattr(dep_obj, "version",       None),
            status       = _val(dep_obj, "status"),
            pipeline_url = getattr(dep_obj, "pipeline_url",  None),
            started_at   = getattr(dep_obj, "started_at",    None),
            finished_at  = getattr(dep_obj, "finished_at",   None),
            created_at   = getattr(dep_obj, "created_at",    None) or now,
        )
        self._s.add(row)
        self._s.flush()
        logger.info(
            "[DB][deployment_repo] created deployment_id=%s service=%s branch=%s status=%s",
            row.id, row.service, row.branch, row.status,
        )
        return row

    def update_status(
        self,
        deployment_id: str,
        status       : str,
        pipeline_url : Optional[str]      = None,
        finished_at  : Optional[datetime] = None,
    ) -> None:
        values: dict = {"status": status}
        if pipeline_url:
            values["pipeline_url"] = pipeline_url
        if finished_at:
            # Ensure timezone-aware
            if finished_at.tzinfo is None:
                finished_at = finished_at.replace(tzinfo=timezone.utc)
            values["finished_at"] = finished_at
        self._s.execute(
            update(DeploymentModel)
            .where(DeploymentModel.id == deployment_id)
            .values(**values)
        )
        logger.info(
            "[DB][deployment_repo] status updated deployment_id=%s new_status=%s",
            deployment_id, status,
        )

    def get_by_id(self, deployment_id: str) -> Optional[DeploymentModel]:
        return self._s.get(DeploymentModel, deployment_id)

    def list_by_project(
        self,
        project_id: str,
        service   : Optional[str] = None,
    ) -> List[DeploymentModel]:
        stmt = select(DeploymentModel).where(DeploymentModel.project_id == project_id)
        if service:
            stmt = stmt.where(DeploymentModel.service == service)
        stmt = stmt.order_by(DeploymentModel.created_at.desc())
        return list(self._s.scalars(stmt).all())

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