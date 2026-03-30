"""
agents/cicd_agent/rollback_manager.py
=======================================
Thin wrapper around provider.rollback() that adds logging and a
uniform RollbackOutcome result the PipelineManager and CICDAgent can rely on.

For KubernetesProvider this calls:
    - kubectl rollout undo deployment/<service>  (if no specific version)
    - image patch to <image_repo>:<version>      (if version is given)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from core.models import DeploymentStatus
from providers.cicd.base_provider import BaseCICDProvider, RollbackResult

logger = logging.getLogger(__name__)


@dataclass
class RollbackOutcome:
    service:        str
    version:        str
    environment:    str
    success:        bool
    message:        str
    deployment_id:  str
    started_at:     datetime
    finished_at:    datetime


class RollbackManager:
    """Executes and logs rollback operations via the active provider."""

    def __init__(self, provider: BaseCICDProvider):
        self.provider = provider

    async def rollback(
        self,
        service:     str,
        version:     str,
        environment: str = "production",
    ) -> RollbackOutcome:
        started = datetime.utcnow()
        logger.info(
            f"RollbackManager: service={service} version={version} env={environment}"
        )

        try:
            result: RollbackResult = await self.provider.rollback(
                service     = service,
                version     = version,
                environment = environment,
            )
            success = result.status in (
                DeploymentStatus.ROLLED_BACK,
                DeploymentStatus.SUCCESS,
            )

            outcome = RollbackOutcome(
                service       = service,
                version       = version,
                environment   = environment,
                success       = success,
                message       = result.message,
                deployment_id = result.deployment_id,
                started_at    = started,
                finished_at   = datetime.utcnow(),
            )

            if success:
                logger.info(f"Rollback succeeded: {service} → {version}")
            else:
                logger.warning(f"Rollback reported failure: {result.message}")

            return outcome

        except Exception as exc:
            logger.error(f"RollbackManager.rollback raised: {exc}", exc_info=True)
            return RollbackOutcome(
                service       = service,
                version       = version,
                environment   = environment,
                success       = False,
                message       = str(exc),
                deployment_id = "",
                started_at    = started,
                finished_at   = datetime.utcnow(),
            )