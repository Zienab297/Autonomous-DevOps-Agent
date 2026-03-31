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
        """
        Fetch the full text logs for a workflow run so the MonitoringAgent
        can find syntax errors, tracebacks, and other failure details.

        Strategy
        --------
        1. Call GET /actions/runs/{run_id}/jobs  → get job list + step summaries
        2. For each job, call GET /actions/jobs/{job_id}/logs  → raw text log
           (GitHub redirects to a pre-signed S3 URL — aiohttp follows it)
        3. Parse raw log lines and include them in the output
        4. Always prepend the structured step-conclusion lines so the
           _check_cicd_conclusion detector still fires on conclusion=failure

        Falls back gracefully: if the zip/log fetch fails for any job,
        we still return the step-summary lines for that job.
        """
        import io
        import zipfile

        full = self._full_repo(repo)
        s    = await self._get_session()

        # ── Step 1: get job list ──────────────────────────────────────────────
        async with s.get(
            f"{self.BASE}/repos/{full}/actions/runs/{run_id}/jobs"
        ) as resp:
            if resp.status != 200:
                logger.warning(
                    "[GitHubProvider] collect_logs: jobs endpoint returned %d for run %s",
                    resp.status, run_id,
                )
                return []
            jobs_data = await resp.json()

        all_lines: list[str] = []

        for job in jobs_data.get("jobs", []):
            job_id   = job.get("id")
            job_name = job.get("name", str(job_id))

            # ── Step 2a: structured summary line (keeps _check_cicd_conclusion working) ──
            all_lines.append(
                f"[{job_name}] status={job['status']} "
                f"conclusion={job.get('conclusion')}"
            )
            for step in job.get("steps", []):
                all_lines.append(
                    f"  step={step['name']} "
                    f"conclusion={step.get('conclusion')}"
                )

            # ── Step 2b: fetch actual log text for this job ───────────────────
            if not job_id:
                continue
            try:
                log_lines = await self._fetch_job_log_lines(full, job_id, job_name)
                all_lines.extend(log_lines)
            except Exception as exc:
                logger.warning(
                    "[GitHubProvider] collect_logs: could not fetch log for job %s (%s): %s",
                    job_name, job_id, exc,
                )

        logger.info(
            "[GitHubProvider] collect_logs: run=%s repo=%s → %d lines",
            run_id, full, len(all_lines),
        )
        return all_lines

    async def _fetch_job_log_lines(
        self, full_repo: str, job_id: int, job_name: str
    ) -> list[str]:
        """
        Fetch raw log text for a single job.

        GitHub returns a 302 redirect to a pre-signed URL containing plain
        text logs (NOT a zip at the job level — only the run-level endpoint
        returns a zip).  aiohttp follows redirects automatically.

        Returns a list of stripped, non-empty log lines prefixed with the
        job name so the MonitoringAgent can trace which job they came from.
        """
        s   = await self._get_session()
        url = f"{self.BASE}/repos/{full_repo}/actions/jobs/{job_id}/logs"

        async with s.get(url, allow_redirects=True) as resp:
            if resp.status not in (200, 302):
                logger.debug(
                    "[GitHubProvider] job log fetch returned %d for job %s",
                    resp.status, job_id,
                )
                return []
            raw_text = await resp.text(encoding="utf-8", errors="replace")

        lines: list[str] = []
        for raw_line in raw_text.splitlines():
            # GitHub prefixes every line with a timestamp: "2024-03-24T02:13:50.1234567Z "
            # Strip it so the detector regexes match cleanly.
            line = raw_line.strip()
            if not line:
                continue
            # Remove the leading timestamp (fixed 29-char ISO prefix + space)
            if len(line) > 29 and line[4] == "-" and line[7] == "-" and "T" in line[:20]:
                line = line[29:].strip()
            if line:
                lines.append(line)

        logger.debug(
            "[GitHubProvider] job '%s' → %d log lines", job_name, len(lines)
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