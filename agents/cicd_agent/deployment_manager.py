import asyncio
import logging
from datetime import datetime

from agents.cicd_agent.models import Deployment, DeploymentStatus
from providers.cicd.base_provider import BaseCICDProvider

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 15
MAX_POLL_ATTEMPTS = 40  # 10 min


class DeploymentManager:
    """Manages the full deploy lifecycle."""

    def __init__(self, provider: BaseCICDProvider):
        self.provider = provider

    async def deploy_and_wait(
        self,
        service: str,
        branch: str,
        version: str | None = None,
        poll: bool = True,
    ) -> Deployment:
        logger.info(f"Deploying {service}@{branch} version={version}")
        deployment = await self.provider.deploy(service, branch, version)
        logger.info(f"Deployment started: id={deployment.id}")

        if not poll:
            return deployment

        return await self._poll_until_done(deployment)

    async def _poll_until_done(self, deployment: Deployment) -> Deployment:
        for _ in range(MAX_POLL_ATTEMPTS):
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            updated = await self.provider.get_deployment_status(deployment.id)
            logger.debug(f"Deployment {deployment.id} status: {updated.status}")
            if updated.status in (
                DeploymentStatus.SUCCESS,
                DeploymentStatus.FAILED,
                DeploymentStatus.ROLLED_BACK,
            ):
                updated.finished_at = datetime.utcnow()
                logger.info(f"Deployment {deployment.id} finished: {updated.status}")
                return updated

        logger.warning(f"Deployment {deployment.id} timed out")
        deployment.status = DeploymentStatus.FAILED
        return deployment

    async def get_logs(self, deployment_id: str) -> list[str]:
        log = await self.provider.get_deployment_logs(deployment_id)
        return log.lines