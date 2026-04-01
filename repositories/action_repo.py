"""repositories/action_repo.py — RemediationAction persistence."""

import logging
import re
from datetime import datetime, timezone
from typing   import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from models.action import ActionModel

logger = logging.getLogger(__name__)

_SENSITIVE = re.compile(
    r"(docker\s+login|DOCKER_PASSWORD|DOCKER_USERNAME|secret|token|password|credentials)",
    re.IGNORECASE,
)


def _mask(cmd: Optional[str]) -> str:
    if not cmd:
        return ""
    return re.sub(r"(=\s*|:\s*)(\S+)", r"\1***", cmd)[:120]


class ActionRepository:

    def __init__(self, session: Session):
        self._s = session

    def create(self, action_obj, project_id: str, incident_id: str) -> ActionModel:
        status  = getattr(action_obj, "status", "pending")
        if hasattr(status, "value"):
            status = status.value
        command = getattr(action_obj, "command", None)
        now     = datetime.now(timezone.utc)

        if command and _SENSITIVE.search(command) and str(status) == "pending":
            status = "action_required"

        row = ActionModel(
            project_id  = project_id,
            incident_id = incident_id,
            command     = command,
            status      = str(status),
            output      = getattr(action_obj, "output",      None),
            error       = getattr(action_obj, "error",       None),
            created_at  = getattr(action_obj, "created_at",  None) or now,
            executed_at = getattr(action_obj, "executed_at", None),
        )
        self._s.add(row)
        self._s.flush()
        logger.info(
            "[DB][action_repo] created id=%s incident_id=%s status=%s cmd=%s",
            row.id, incident_id, row.status, _mask(command),
        )
        return row

    def get_by_id(self, action_id: int) -> Optional[ActionModel]:
        return self._s.get(ActionModel, action_id)

    def list_by_incident(self, project_id: str, incident_id: str) -> List[ActionModel]:
        stmt = (
            select(ActionModel)
            .where(
                ActionModel.project_id  == project_id,
                ActionModel.incident_id == incident_id,
            )
            .order_by(ActionModel.created_at.desc())
        )
        return list(self._s.scalars(stmt).all())

    def list_recent(self, project_id: str, limit: int = 20) -> List[ActionModel]:
        stmt = (
            select(ActionModel)
            .where(ActionModel.project_id == project_id)
            .order_by(ActionModel.created_at.desc())
            .limit(limit)
        )
        return list(self._s.scalars(stmt).all())