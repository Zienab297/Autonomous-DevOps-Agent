from abc import ABC, abstractmethod
from agents.cicd_agent.models import Pipeline, Deployment, RollbackResult, DeploymentLog


class BaseCICDProvider(ABC):
    """Abstract interface every CI/CD provider must implement."""

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    async def trigger_pipeline(self, repo: str, branch: str, inputs: dict | None = None) -> Pipeline:
        """Trigger a CI pipeline and return a Pipeline object."""

    @abstractmethod
    async def get_pipeline_status(self, pipeline_id: str) -> Pipeline:
        """Fetch current status of a pipeline run."""

    @abstractmethod
    async def deploy(self, service: str, branch: str, version: str | None = None) -> Deployment:
        """Deploy a service from the given branch/version."""

    @abstractmethod
    async def get_deployment_status(self, deployment_id: str) -> Deployment:
        """Fetch current status of a deployment."""

    @abstractmethod
    async def rollback(self, service: str, to_version: str) -> RollbackResult:
        """Roll back a service to a specific version."""

    @abstractmethod
    async def get_deployment_logs(self, deployment_id: str) -> DeploymentLog:
        """Retrieve logs for a deployment."""

    @abstractmethod
    async def list_deployments(self, service: str, limit: int = 10) -> list[Deployment]:
        """List recent deployments for a service."""