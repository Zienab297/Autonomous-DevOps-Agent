"""
providers/cicd/github_provider.py
===================================
GitHub Actions + GitHub Deployments REST API.
Docs: https://docs.github.com/en/rest/actions

Environment variables:
    GITHUB_TOKEN  — personal access token or GitHub App token
    GITHUB_ORG    — default org/owner (optional if repo already contains owner/)

Returns core.models.Deployment from deploy() and rollback(),
so results drop directly into StateManager and ContextManager.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from typing import Any, Optional

import aiohttp

from core.models import Deployment, DeploymentStatus
from providers.cicd.base_provider import BaseCICDProvider, PipelineRun, RollbackResult

logger = logging.getLogger(__name__)


# ── status translation maps ───────────────────────────────────────────────────

_RUN_STATUS: dict[str, str] = {
    "queued":      "pending",
    "in_progress": "running",
    "completed":   "success",   # conclusion overrides this below
    "failure":     "failed",
    "cancelled":   "cancelled",
    "success":     "success",
    "timed_out":   "failed",
    "skipped":     "cancelled",
    "neutral":     "success",
    "waiting":     "pending",
}

_DEP_STATUS: dict[str, DeploymentStatus] = {
    "success":     DeploymentStatus.SUCCESS,
    "failure":     DeploymentStatus.FAILED,
    "error":       DeploymentStatus.FAILED,
    "in_progress": DeploymentStatus.RUNNING,
    "queued":      DeploymentStatus.PENDING,
    "pending":     DeploymentStatus.PENDING,
    "inactive":    DeploymentStatus.FAILED,
}


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class GitHubProvider(BaseCICDProvider):
    """
    Implements BaseCICDProvider against the GitHub REST API.

    Pipeline operations use GitHub Actions workflow_dispatch + runs API.
    Deployment operations use the GitHub Deployments API.
    """

    BASE = "https://api.github.com"

    def __init__(
        self,
        token:   Optional[str] = None,
        org:     Optional[str] = None,
        timeout: int = 30,
    ):
        self._token   = token or os.getenv("GITHUB_TOKEN", "")
        self._org     = org   or os.getenv("GITHUB_ORG", "")
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: Optional[aiohttp.ClientSession] = None

    # ── identity ──────────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "github"

    # ── HTTP session ──────────────────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        return {
            "Accept":               "application/vnd.github+json",
            "Authorization":        f"Bearer {self._token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers=self._headers(),
                timeout=self._timeout,
            )
        return self._session

    async def close(self) -> None:
        """Close the HTTP session. Called by CICDAgent._teardown()."""
        if self._session and not self._session.closed:
            await self._session.close()

    def _full_repo(self, repo: str) -> str:
        """Ensure 'owner/repo' format."""
        return repo if "/" in repo else f"{self._org}/{repo}"

    # ── pipeline ──────────────────────────────────────────────────────────────

    async def trigger_pipeline(
        self,
        repo:     str,
        branch:   str = "main",
        workflow: str = "deploy.yml",
        inputs:   dict[str, Any] | None = None,
    ) -> PipelineRun:
        full = self._full_repo(repo)
        wf   = workflow or "deploy.yml"
        url  = f"{self.BASE}/repos/{full}/actions/workflows/{wf}/dispatches"
        body: dict[str, Any] = {"ref": branch}
        if inputs:
            body["inputs"] = inputs

        s = await self._get_session()
        async with s.post(url, json=body) as resp:
            if resp.status not in (200, 204):
                text = await resp.text()
                raise RuntimeError(
                    f"GitHub trigger_pipeline failed [{resp.status}]: {text}"
                )

        # Brief pause so GitHub registers the run before we fetch it
        await asyncio.sleep(2)
        runs = await self._list_recent_runs(full, wf, branch)
        run  = runs[0] if runs else {}

        return PipelineRun(
            id         = str(run.get("id", "unknown")),
            repo       = full,
            branch     = branch,
            workflow   = wf,
            status     = _RUN_STATUS.get(run.get("status", ""), "pending"),
            url        = run.get("html_url", ""),
            started_at = _parse_dt(run.get("created_at")),
            raw        = run,
        )

    async def _list_recent_runs(
        self, repo: str, workflow: str, branch: str
    ) -> list[dict]:
        url = f"{self.BASE}/repos/{repo}/actions/workflows/{workflow}/runs"
        s   = await self._get_session()
        async with s.get(url, params={"branch": branch, "per_page": 5}) as resp:
            if resp.status != 200:
                return []
            data = await resp.json()
            return data.get("workflow_runs", [])

    async def get_pipeline_status(self, run_id: str, repo: str) -> PipelineRun:
        full = self._full_repo(repo)
        s    = await self._get_session()
        async with s.get(f"{self.BASE}/repos/{full}/actions/runs/{run_id}") as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(
                    f"GitHub get_pipeline_status failed [{resp.status}]: {text}"
                )
            data = await resp.json()

        # conclusion takes priority over status for completed runs
        raw_status = data.get("conclusion") or data.get("status", "")
        return PipelineRun(
            id          = run_id,
            repo        = full,
            branch      = data.get("head_branch", ""),
            workflow    = data.get("name", ""),
            status      = _RUN_STATUS.get(raw_status, "unknown"),
            url         = data.get("html_url", ""),
            started_at  = _parse_dt(data.get("created_at")),
            finished_at = _parse_dt(data.get("updated_at")),
            raw         = data,
        )

    async def collect_logs(self, run_id: str, repo: str) -> list[str]:
        """Return job names + step conclusions (avoids large zip download)."""
        full = self._full_repo(repo)
        s    = await self._get_session()
        async with s.get(
            f"{self.BASE}/repos/{full}/actions/runs/{run_id}/jobs"
        ) as resp:
            if resp.status != 200:
                return []
            data = await resp.json()

        lines = []
        for job in data.get("jobs", []):
            lines.append(
                f"[{job['name']}] status={job['status']} "
                f"conclusion={job.get('conclusion')}"
            )
            for step in job.get("steps", []):
                lines.append(
                    f"  step={step['name']} "
                    f"conclusion={step.get('conclusion')}"
                )
        return lines

    # ── deployment ────────────────────────────────────────────────────────────

    async def deploy(
        self,
        service:     str,
        branch:      str = "main",
        environment: str = "production",
        version:     str = "",
    ) -> Deployment:
        """
        Create a GitHub Deployment and mark it in_progress.
        Returns core.models.Deployment for StateManager storage.
        """
        full = self._full_repo(service)
        s    = await self._get_session()
        body = {
            "ref":               version or branch,
            "environment":       environment,
            "auto_merge":        False,
            "required_contexts": [],
            "description":       "Deployed via DevOps Agent SDK",
        }
        async with s.post(
            f"{self.BASE}/repos/{full}/deployments", json=body
        ) as resp:
            if resp.status not in (201, 202):
                text = await resp.text()
                raise RuntimeError(
                    f"GitHub deploy failed [{resp.status}]: {text}"
                )
            data = await resp.json()

        gh_id = str(data["id"])
        await self._set_deployment_status(full, gh_id, "in_progress", environment)

        return Deployment(
            service       = service,
            branch        = branch,
            version       = version or branch,
            deployment_id = f"DEP-GH-{gh_id}",
            status        = DeploymentStatus.RUNNING,
            pipeline_url  = data.get("url", ""),
            started_at    = _parse_dt(data.get("created_at")),
            metadata      = {
                "provider":    "github",
                "github_id":   gh_id,
                "environment": environment,
                "repo":        full,
            },
        )

    async def _set_deployment_status(
        self, repo: str, dep_id: str, state: str, environment: str
    ) -> None:
        s = await self._get_session()
        async with s.post(
            f"{self.BASE}/repos/{repo}/deployments/{dep_id}/statuses",
            json={"state": state, "environment": environment},
        ) as resp:
            if resp.status not in (201, 202):
                logger.warning(
                    f"Could not set GitHub deployment status to '{state}': "
                    f"{resp.status}"
                )

    async def get_deployment_status(self, deployment_id: str) -> Deployment:
        """
        deployment_id format: "owner/repo:github_deployment_id"
        e.g. "my-org/auth-api:987654321"
        """
        parts = deployment_id.split(":")
        if len(parts) != 2:
            raise ValueError(
                "deployment_id must be 'owner/repo:github_deployment_id'"
            )
        repo, gh_id = parts
        s = await self._get_session()
        async with s.get(
            f"{self.BASE}/repos/{repo}/deployments/{gh_id}/statuses"
        ) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(
                    f"GitHub get_deployment_status failed [{resp.status}]: {text}"
                )
            statuses = await resp.json()

        latest = statuses[0] if statuses else {}
        return Deployment(
            service       = repo,
            branch        = "",
            version       = "",
            deployment_id = deployment_id,
            status        = _DEP_STATUS.get(
                latest.get("state", ""), DeploymentStatus.PENDING
            ),
            pipeline_url  = latest.get("url", ""),
            metadata      = {"provider": "github", "github_id": gh_id},
        )

    async def rollback(
        self,
        service:     str,
        version:     str,
        environment: str = "production",
    ) -> RollbackResult:
        """
        Rollback by creating a new deployment pointing to the old version.
        Marks the resulting GitHub deployment as success immediately.
        """
        deployment = await self.deploy(
            service=service, branch=version,
            environment=environment, version=version,
        )
        gh_id = deployment.metadata.get("github_id", "")
        full  = self._full_repo(service)
        if gh_id:
            await self._set_deployment_status(full, gh_id, "success", environment)

        return RollbackResult(
            deployment_id  = deployment.deployment_id,
            service        = service,
            rolled_back_to = version,
            status         = DeploymentStatus.ROLLED_BACK,
            message        = (
                f"Rolled back {service} to {version} "
                f"on {environment} via GitHub Deployments"
            ),
            raw            = deployment.metadata,
        )

    # ── health ────────────────────────────────────────────────────────────────

    async def health_check(self) -> bool:
        try:
            s = await self._get_session()
            async with s.get(f"{self.BASE}/user") as resp:
                return resp.status == 200
        except Exception:
            return False