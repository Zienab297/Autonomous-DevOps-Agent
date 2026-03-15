import logging
from typing import Any

from core.base_agent import BaseAgent, AgentEvent
from agents.cicd_agent.models import Deployment, DeploymentStatus, Pipeline, RollbackResult
from agents.cicd_agent.pipeline_manager import PipelineManager
from agents.cicd_agent.deployment_manager import DeploymentManager
from agents.cicd_agent.rollback_manager import RollbackManager
from providers.cicd.base_provider import BaseCICDProvider

logger = logging.getLogger(__name__)


class CICDAgent(BaseAgent):
    """
    Autonomous CI/CD Agent.

    Responsibilities:
    - Trigger and monitor CI pipelines
    - Deploy services
    - Roll back on failure
    - Collect deployment logs
    - Respond to orchestrator events
    """

    def __init__(self, provider: BaseCICDProvider):
        super().__init__("cicd_agent")
        self.provider = provider
        self.pipeline_manager = PipelineManager(provider)
        self.deployment_manager = DeploymentManager(provider)
        self.rollback_manager = RollbackManager(provider)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def trigger_pipeline(
        self,
        repo: str,
        branch: str = "main",
        inputs: dict | None = None,
        wait: bool = False,
    ) -> Pipeline:
        """Trigger a CI pipeline, optionally waiting for completion."""
        return await self.pipeline_manager.trigger_and_wait(
            repo, branch, inputs, poll=wait
        )

    async def deploy(
        self,
        service: str,
        branch: str = "main",
        version: str | None = None,
        wait: bool = False,
    ) -> Deployment:
        """Deploy a service, optionally waiting for completion."""
        return await self.deployment_manager.deploy_and_wait(
            service, branch, version, poll=wait
        )

    async def rollback(self, service: str, to_version: str) -> RollbackResult:
        """Roll back a service to a specific version."""
        return await self.rollback_manager.rollback_to_version(service, to_version)

    async def rollback_to_previous(self, service: str) -> RollbackResult:
        """Roll back to the last known good deployment."""
        return await self.rollback_manager.rollback_to_previous(service)

    async def collect_deployment_logs(self, deployment_id: str) -> list[str]:
        """Fetch logs for a deployment."""
        return await self.deployment_manager.get_logs(deployment_id)

    async def list_deployments(self, service: str, limit: int = 10) -> list[Deployment]:
        """List recent deployments for a service."""
        return await self.provider.list_deployments(service, limit)

    # ------------------------------------------------------------------
    # Event handling (called by Orchestrator)
    # ------------------------------------------------------------------

    async def handle_event(self, event: AgentEvent) -> Any:
        logger.info(f"CICDAgent handling event: {event.type}")

        handlers = {
            "deploy_requested": self._on_deploy_requested,
            "rollback_requested": self._on_rollback_requested,
            "pipeline_requested": self._on_pipeline_requested,
        }

        handler = handlers.get(event.type)
        if handler:
            return await handler(event)

        logger.warning(f"Unhandled event type: {event.type}")
        return None

    async def _on_deploy_requested(self, event: AgentEvent) -> Deployment:
        return await self.deploy(
            service=event.payload["service"],
            branch=event.payload.get("branch", "main"),
            version=event.payload.get("version"),
            wait=event.payload.get("wait", False),
        )

    async def _on_rollback_requested(self, event: AgentEvent) -> RollbackResult:
        service = event.payload["service"]
        to_version = event.payload.get("to_version")
        if to_version:
            return await self.rollback(service, to_version)
        return await self.rollback_to_previous(service)

    async def _on_pipeline_requested(self, event: AgentEvent) -> Pipeline:
        return await self.trigger_pipeline(
            repo=event.payload["repo"],
            branch=event.payload.get("branch", "main"),
            inputs=event.payload.get("inputs"),
            wait=event.payload.get("wait", False),
        )