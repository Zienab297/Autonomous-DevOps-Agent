import logging
from datetime import datetime

import httpx

from agents.cicd_agent.models import (
    Deployment,
    DeploymentLog,
    DeploymentStatus,
    Pipeline,
    PipelineStatus,
    RollbackResult,
)
from .base_provider import BaseCICDProvider

logger = logging.getLogger(__name__)


class GitLabProvider(BaseCICDProvider):
    """
    GitLab CI/CD provider.

    Uses the GitLab REST API v4:
      https://docs.gitlab.com/ee/api/pipelines.html
    """

    def __init__(self, token: str, base_url: str = "https://gitlab.com", timeout: int = 30):
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=f"{self._base_url}/api/v4",
            headers={"PRIVATE-TOKEN": token},
            timeout=timeout,
        )

    @property
    def name(self) -> str:
        return "gitlab"

    async def trigger_pipeline(
        self, repo: str, branch: str, inputs: dict | None = None
    ) -> Pipeline:
        """Trigger a pipeline via pipeline trigger token or API."""
        encoded = repo.replace("/", "%2F")
        payload = {"ref": branch, **(inputs or {})}
        resp = await self._client.post(f"/projects/{encoded}/pipeline", json=payload)
        self._raise_for_status(resp, f"trigger pipeline {repo}@{branch}")
        data = resp.json()
        return self._parse_pipeline(data, repo)

    async def get_pipeline_status(self, pipeline_id: str) -> Pipeline:
        # pipeline_id expected as "namespace/repo:id"
        repo, pid = self._split_id(pipeline_id)
        encoded = repo.replace("/", "%2F")
        resp = await self._client.get(f"/projects/{encoded}/pipelines/{pid}")
        self._raise_for_status(resp, f"get pipeline {pipeline_id}")
        return self._parse_pipeline(resp.json(), repo)

    async def deploy(
        self, service: str, branch: str, version: str | None = None
    ) -> Deployment:
        # GitLab: trigger a deploy pipeline
        dep = await self.trigger_pipeline(service, version or branch)
        return Deployment(
            id=dep.id,
            service=service,
            branch=branch,
            version=version or branch,
            status=DeploymentStatus.IN_PROGRESS,
            provider=self.name,
        )

    async def get_deployment_status(self, deployment_id: str) -> Deployment:
        pipeline = await self.get_pipeline_status(deployment_id)
        status_map = {
            PipelineStatus.PENDING: DeploymentStatus.PENDING,
            PipelineStatus.RUNNING: DeploymentStatus.IN_PROGRESS,
            PipelineStatus.SUCCESS: DeploymentStatus.SUCCESS,
            PipelineStatus.FAILED: DeploymentStatus.FAILED,
            PipelineStatus.CANCELLED: DeploymentStatus.FAILED,
        }
        return Deployment(
            id=pipeline.id,
            service=pipeline.repo,
            branch=pipeline.branch,
            version=pipeline.branch,
            status=status_map.get(pipeline.status, DeploymentStatus.PENDING),
            provider=self.name,
        )

    async def rollback(self, service: str, to_version: str) -> RollbackResult:
        dep = await self.deploy(service, branch=to_version, version=to_version)
        return RollbackResult(
            deployment_id=dep.id,
            service=service,
            from_version="current",
            to_version=to_version,
            status=DeploymentStatus.IN_PROGRESS,
            message=f"Rollback pipeline triggered — id {dep.id}",
        )

    async def get_deployment_logs(self, deployment_id: str) -> DeploymentLog:
        repo, pid = self._split_id(deployment_id)
        encoded = repo.replace("/", "%2F")
        resp = await self._client.get(f"/projects/{encoded}/pipelines/{pid}/jobs")
        self._raise_for_status(resp, f"list jobs {pid}")
        jobs = resp.json()
        lines: list[str] = []
        for job in jobs:
            job_id = job["id"]
            log_resp = await self._client.get(
                f"/projects/{encoded}/jobs/{job_id}/trace"
            )
            if not log_resp.is_error:
                lines.extend(log_resp.text.splitlines())
        return DeploymentLog(deployment_id=pid, lines=lines)

    async def list_deployments(self, service: str, limit: int = 10) -> list[Deployment]:
        encoded = service.replace("/", "%2F")
        resp = await self._client.get(
            f"/projects/{encoded}/pipelines",
            params={"per_page": limit, "order_by": "id", "sort": "desc"},
        )
        self._raise_for_status(resp, f"list deployments {service}")
        return [
            Deployment(
                id=str(item["id"]),
                service=service,
                branch=item.get("ref", "unknown"),
                version=item.get("sha", "unknown"),
                status=DeploymentStatus.PENDING,
                provider=self.name,
                deployed_at=datetime.fromisoformat(
                    item["created_at"].replace("Z", "+00:00")
                ),
            )
            for item in resp.json()
        ]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _parse_pipeline(self, data: dict, repo: str) -> Pipeline:
        return Pipeline(
            id=str(data["id"]),
            repo=repo,
            branch=data.get("ref", "unknown"),
            status=self._map_status(data.get("status", "pending")),
            provider=self.name,
            triggered_at=datetime.fromisoformat(
                data["created_at"].replace("Z", "+00:00")
            ),
            logs_url=data.get("web_url"),
        )

    @staticmethod
    def _map_status(status: str) -> PipelineStatus:
        mapping = {
            "created": PipelineStatus.PENDING,
            "waiting_for_resource": PipelineStatus.PENDING,
            "preparing": PipelineStatus.PENDING,
            "pending": PipelineStatus.PENDING,
            "running": PipelineStatus.RUNNING,
            "success": PipelineStatus.SUCCESS,
            "failed": PipelineStatus.FAILED,
            "canceled": PipelineStatus.CANCELLED,
            "skipped": PipelineStatus.CANCELLED,
        }
        return mapping.get(status, PipelineStatus.PENDING)

    @staticmethod
    def _split_id(combined_id: str) -> tuple[str, str]:
        if ":" in combined_id:
            repo, pid = combined_id.split(":", 1)
            return repo, pid
        return "unknown", combined_id

    @staticmethod
    def _raise_for_status(resp: httpx.Response, context: str) -> None:
        if resp.is_error:
            raise RuntimeError(
                f"GitLab API error [{context}]: {resp.status_code} — {resp.text}"
            )

    async def close(self) -> None:
        await self._client.aclose()