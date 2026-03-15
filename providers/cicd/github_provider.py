import logging
import uuid
from datetime import datetime
from typing import Any

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


class GitHubProvider(BaseCICDProvider):
    """
    GitHub Actions CI/CD provider.

    Uses the GitHub REST API v3:
      https://docs.github.com/en/rest/actions
    """

    BASE_URL = "https://api.github.com"

    def __init__(self, token: str, org: str, timeout: int = 30):
        self._token = token
        self._org = org
        self._client = httpx.AsyncClient(
            base_url=self.BASE_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=timeout,
        )

    @property
    def name(self) -> str:
        return "github"

    # ------------------------------------------------------------------
    # Pipelines
    # ------------------------------------------------------------------

    async def trigger_pipeline(
        self, repo: str, branch: str, inputs: dict | None = None
    ) -> Pipeline:
        """Dispatch a workflow_dispatch event on the default CI workflow."""
        payload: dict[str, Any] = {"ref": branch, "inputs": inputs or {}}
        resp = await self._client.post(
            f"/repos/{self._org}/{repo}/actions/workflows/ci.yml/dispatches",
            json=payload,
        )
        self._raise_for_status(resp, f"trigger pipeline {repo}@{branch}")

        # GitHub returns 204 — we synthesise an id and poll later
        run_id = await self._get_latest_run_id(repo, branch)
        return Pipeline(
            id=str(run_id),
            repo=repo,
            branch=branch,
            status=PipelineStatus.RUNNING,
            provider=self.name,
            logs_url=f"https://github.com/{self._org}/{repo}/actions/runs/{run_id}",
        )

    async def get_pipeline_status(self, pipeline_id: str) -> Pipeline:
        # pipeline_id == GitHub Actions run_id
        # We need repo context; store it in metadata if needed.
        # For simplicity we look up by run id across the org.
        resp = await self._client.get(f"/repos/{self._org}/_unknown/actions/runs/{pipeline_id}")
        self._raise_for_status(resp, f"get pipeline {pipeline_id}")
        data = resp.json()
        return self._parse_run(data)

    # ------------------------------------------------------------------
    # Deployments  (modelled as GitHub Deployments API)
    # ------------------------------------------------------------------

    async def deploy(
        self, service: str, branch: str, version: str | None = None
    ) -> Deployment:
        ref = version or branch
        payload = {
            "ref": ref,
            "environment": "production",
            "description": f"Deploy {service} from {ref}",
            "auto_merge": False,
            "required_contexts": [],
        }
        resp = await self._client.post(
            f"/repos/{self._org}/{service}/deployments", json=payload
        )
        self._raise_for_status(resp, f"deploy {service}@{ref}")
        data = resp.json()
        dep_id = str(data["id"])
        sha = data.get("sha", ref)

        # Create a "in_progress" deployment status
        await self._set_deployment_status(service, dep_id, "in_progress")

        return Deployment(
            id=dep_id,
            service=service,
            branch=branch,
            version=sha,
            status=DeploymentStatus.IN_PROGRESS,
            provider=self.name,
        )

    async def get_deployment_status(self, deployment_id: str) -> Deployment:
        # Without knowing the repo we can't call the API — callers should
        # pass service name; here we accept "service:id" format.
        service, dep_id = self._split_deployment_id(deployment_id)
        resp = await self._client.get(
            f"/repos/{self._org}/{service}/deployments/{dep_id}/statuses"
        )
        self._raise_for_status(resp, f"get deployment status {dep_id}")
        statuses = resp.json()
        latest = statuses[0] if statuses else {}
        gh_state = latest.get("state", "pending")
        return Deployment(
            id=dep_id,
            service=service,
            branch="unknown",
            version="unknown",
            status=self._map_gh_deployment_status(gh_state),
            provider=self.name,
        )

    async def rollback(self, service: str, to_version: str) -> RollbackResult:
        # Re-deploy the target SHA/tag
        dep = await self.deploy(service, branch=to_version, version=to_version)
        return RollbackResult(
            deployment_id=dep.id,
            service=service,
            from_version="current",
            to_version=to_version,
            status=DeploymentStatus.IN_PROGRESS,
            message=f"Rollback initiated — deployment id {dep.id}",
        )

    async def get_deployment_logs(self, deployment_id: str) -> DeploymentLog:
        service, dep_id = self._split_deployment_id(deployment_id)
        resp = await self._client.get(
            f"/repos/{self._org}/{service}/actions/runs/{dep_id}/logs"
        )
        self._raise_for_status(resp, f"get logs {dep_id}")
        # GitHub returns a ZIP; we return the raw bytes as a single line
        return DeploymentLog(
            deployment_id=dep_id,
            lines=[f"[binary log blob — {len(resp.content)} bytes]"],
        )

    async def list_deployments(self, service: str, limit: int = 10) -> list[Deployment]:
        resp = await self._client.get(
            f"/repos/{self._org}/{service}/deployments",
            params={"per_page": limit},
        )
        self._raise_for_status(resp, f"list deployments {service}")
        items = resp.json()
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
            for item in items
        ]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _get_latest_run_id(self, repo: str, branch: str) -> int:
        resp = await self._client.get(
            f"/repos/{self._org}/{repo}/actions/runs",
            params={"branch": branch, "per_page": 1},
        )
        self._raise_for_status(resp, "get latest run")
        runs = resp.json().get("workflow_runs", [])
        return runs[0]["id"] if runs else 0

    async def _set_deployment_status(
        self, service: str, dep_id: str, state: str
    ) -> None:
        await self._client.post(
            f"/repos/{self._org}/{service}/deployments/{dep_id}/statuses",
            json={"state": state},
        )

    def _parse_run(self, data: dict) -> Pipeline:
        gh_conclusion = data.get("conclusion")
        gh_status = data.get("status", "queued")
        status = self._map_gh_run_status(gh_status, gh_conclusion)
        return Pipeline(
            id=str(data["id"]),
            repo=data.get("repository", {}).get("name", "unknown"),
            branch=data.get("head_branch", "unknown"),
            status=status,
            provider=self.name,
            triggered_at=datetime.fromisoformat(
                data["created_at"].replace("Z", "+00:00")
            ),
            logs_url=data.get("html_url"),
        )

    @staticmethod
    def _map_gh_run_status(status: str, conclusion: str | None) -> PipelineStatus:
        if status in ("queued", "waiting"):
            return PipelineStatus.PENDING
        if status == "in_progress":
            return PipelineStatus.RUNNING
        if conclusion == "success":
            return PipelineStatus.SUCCESS
        if conclusion in ("failure", "timed_out"):
            return PipelineStatus.FAILED
        if conclusion == "cancelled":
            return PipelineStatus.CANCELLED
        return PipelineStatus.PENDING

    @staticmethod
    def _map_gh_deployment_status(state: str) -> DeploymentStatus:
        mapping = {
            "pending": DeploymentStatus.PENDING,
            "in_progress": DeploymentStatus.IN_PROGRESS,
            "success": DeploymentStatus.SUCCESS,
            "failure": DeploymentStatus.FAILED,
            "error": DeploymentStatus.FAILED,
        }
        return mapping.get(state, DeploymentStatus.PENDING)

    @staticmethod
    def _split_deployment_id(deployment_id: str) -> tuple[str, str]:
        """Expect 'service:id' or just 'id'."""
        if ":" in deployment_id:
            service, dep_id = deployment_id.split(":", 1)
            return service, dep_id
        return "unknown", deployment_id

    @staticmethod
    def _raise_for_status(resp: httpx.Response, context: str) -> None:
        if resp.is_error:
            raise RuntimeError(
                f"GitHub API error [{context}]: {resp.status_code} — {resp.text}"
            )

    async def close(self) -> None:
        await self._client.aclose()