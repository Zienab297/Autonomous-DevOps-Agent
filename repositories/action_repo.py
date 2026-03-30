"""
repositories/action_repo.py
----------------------------
Repository for RemediationAction persistence.

Changes vs original:
- All timestamps use datetime.now(timezone.utc)
- Structured logging includes incident_id + command (masked for credentials)
- "action_required" status supported for sensitive ops (docker login, secrets)
"""

import logging
import re
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from models.action import ActionModel

logger = logging.getLogger(__name__)

# Patterns that indicate a sensitive/credential-bearing command
_SENSITIVE_PATTERNS = re.compile(
    r"(docker\s+login|DOCKER_PASSWORD|DOCKER_USERNAME|ghcr\.io|secret|token|password|credentials)",
    re.IGNORECASE,
)


def _mask_command(cmd: Optional[str]) -> str:
    """Return a loggable version of a command with sensitive values masked."""
    if not cmd:
        return ""
    # Replace anything that looks like a value after = or : with ***
    masked = re.sub(r"(=\s*|:\s*)(\S+)", r"\1***", cmd)
    return masked[:120]


class ActionRepository:

    def __init__(self, session: Session):
        self._s = session

    def create(self, action_obj, project_id: str, incident_id: str) -> ActionModel:
        status  = getattr(action_obj, "status", "pending")
        if hasattr(status, "value"):
            status = status.value

        command = getattr(action_obj, "command", None)
        now     = datetime.now(timezone.utc)

        # Auto-elevate status to action_required for sensitive commands
        if command and _SENSITIVE_PATTERNS.search(command) and str(status) == "pending":
            status = "action_required"
            logger.info(
                "[DB][action_repo] sensitive command detected — "
                "marking action_required incident_id=%s cmd_preview=%s",
                incident_id, _mask_command(command),
            )

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
            "[DB][action_repo] created action id=%s incident_id=%s status=%s cmd_preview=%s",
            row.id, incident_id, row.status, _mask_command(command),
        )
        return row

    def get_by_id(self, action_id: int) -> Optional[ActionModel]:
        return self._s.get(ActionModel, action_id)

    def list_by_project(
        self,
        project_id : str,
        incident_id: Optional[str] = None,
        status     : Optional[str] = None,
    ) -> List[ActionModel]:
        stmt = select(ActionModel).where(ActionModel.project_id == project_id)
        if incident_id:
            stmt = stmt.where(ActionModel.incident_id == incident_id)
        if status:
            stmt = stmt.where(ActionModel.status == status)
        stmt = stmt.order_by(ActionModel.created_at.desc())
        return list(self._s.scalars(stmt).all())

    def list_recent(self, project_id: str, limit: int = 20) -> List[ActionModel]:
        stmt = (
            select(ActionModel)
            .where(ActionModel.project_id == project_id)
            .order_by(ActionModel.created_at.desc())
            .limit(limit)
        )
        return list(self._s.scalars(stmt).all())