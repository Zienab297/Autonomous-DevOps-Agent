"""
providers/cicd/multi_provider.py
---------------------------------
CompositeProvider — runs GitHub Actions AND Kubernetes together.

When deploy() is called:
  1. GitHub Provider creates the Deployment record (audit trail)
  2. Kubernetes Provider performs the actual rolling update

When rollback() is called:
  1. Kubernetes rolls back immediately (fast)
  2. GitHub records the rollback deployment (audit trail)

Either provider can be omitted — the composite degrades gracefully.

Usage
-----
    from providers.cicd.multi_provider import CompositeProvider
    from providers.cicd.github_provider import GitHubProvider
    from providers.cicd.k8s_provider    import KubernetesProvider

    provider = CompositeProvider(
        github = GitHubProvider(token="...", org="Zienab297"),
        k8s    = KubernetesProvider(namespace="production"),
    )

    # Wire into CICDAgent exactly like any other provider
    agent = CICDAgent(provider=provider, ...)
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from core.models import Deployment, DeploymentStatus
from providers.cicd.base_provider import (
    BaseCICDProvider, PipelineRun, RollbackResult,
)

logger = logging.getLogger(__name__)


class CompositeProvider(BaseCICDProvider):
    """
    Combines GitHub Actions + Kubernetes into a single provider.

    GitHub handles: pipeline triggers, deployment records, audit trail
    Kubernetes handles: actual container rollouts, rollbacks, pod logs

    Both can fail independently — errors are logged and the
    other provider's result is returned.
    """

    def __init__(
        self,
        github: Optional[BaseCICDProvider] = None,
        k8s:    Optional[BaseCICDProvider] = None,
    ):
        if not github and not k8s:
            raise ValueError(
                "CompositeProvider requires at least one provider "
                "(github or k8s)"
            )
        self._github = github
        self._k8s    = k8s
        logger.info(
            f"[CompositeProvider] Initialized — "
            f"github={'on' if github else 'off'} "
            f"k8s={'on' if k8s else 'off'}"
        )

    @property
    def name(self) -> str:
        parts = []
        if self._github: parts.append("github")
        if self._k8s:    parts.append("kubernetes")
        return "+".join(parts)

    # ── pipeline (GitHub only — K8s doesn't have pipelines) ──────────────────

    async def trigger_pipeline(
        self,
        repo:     str,
        branch:   str = "main",
        workflow: str = "",
        inputs:   dict | None = None,
    ) -> PipelineRun:
        if self._github:
            return await self._github.trigger_pipeline(
                repo=repo, branch=branch, workflow=workflow, inputs=inputs
            )
        # K8s fallback — restart the deployment
        return await self._k8s.trigger_pipeline(
            repo=repo, branch=branch, workflow=workflow, inputs=inputs
        )

    async def get_pipeline_status(self, run_id: str, repo: str) -> PipelineRun:
        if self._github and not run_id.startswith("k8s-"):
            return await self._github.get_pipeline_status(run_id, repo)
        if self._k8s:
            return await self._k8s.get_pipeline_status(run_id, repo)
        return await self._github.get_pipeline_status(run_id, repo)

    # ── deploy — GitHub records it, K8s executes it ───────────────────────────

    async def deploy(
        self,
        service:     str,
        branch:      str = "main",
        environment: str = "production",
        version:     str = "",
    ) -> Deployment:
        """
        1. K8s performs the rolling update (primary — users see this)
        2. GitHub creates the deployment record (audit trail)
        Returns the K8s result if available, else GitHub's.
        """
        k8s_dep    = None
        github_dep = None

        # Step 1: K8s rolling update
        if self._k8s:
            try:
                k8s_dep = await self._k8s.deploy(
                    service=service,
                    branch=branch,
                    environment=environment,
                    version=version,
                )
                logger.info(
                    f"[CompositeProvider] K8s deploy OK: {k8s_dep.deployment_id}"
                )
            except Exception as exc:
                logger.error(f"[CompositeProvider] K8s deploy failed: {exc}")

        # Step 2: GitHub deployment record (audit trail)
        if self._github:
            try:
                github_dep = await self._github.deploy(
                    service=service,
                    branch=branch,
                    environment=environment,
                    version=version,
                )
                logger.info(
                    f"[CompositeProvider] GitHub deploy record: {github_dep.deployment_id}"
                )
            except Exception as exc:
                logger.warning(
                    f"[CompositeProvider] GitHub record failed (non-fatal): {exc}"
                )

        # Prefer K8s result — it's the real deployment
        result = k8s_dep or github_dep
        if result is None:
            raise RuntimeError(
                "CompositeProvider.deploy(): both GitHub and K8s failed"
            )

        # Merge metadata from both so StateManager has the full picture
        result.metadata["providers"] = self.name
        if k8s_dep and github_dep:
            result.metadata["github_deployment_id"] = github_dep.deployment_id
            result.metadata["k8s_deployment_id"]    = k8s_dep.deployment_id

        return result

    # ── rollback — K8s first (fast), then GitHub record ──────────────────────

    async def rollback(
        self,
        service:     str,
        version:     str,
        environment: str = "production",
    ) -> RollbackResult:
        """
        1. K8s rollout undo (fast — immediate)
        2. GitHub records the rollback deployment
        """
        k8s_result    = None
        github_result = None

        if self._k8s:
            try:
                k8s_result = await self._k8s.rollback(
                    service=service, version=version, environment=environment
                )
                logger.info(
                    f"[CompositeProvider] K8s rollback OK: {k8s_result.rolled_back_to}"
                )
            except Exception as exc:
                logger.error(f"[CompositeProvider] K8s rollback failed: {exc}")

        if self._github:
            try:
                github_result = await self._github.rollback(
                    service=service, version=version, environment=environment
                )
                logger.info(
                    f"[CompositeProvider] GitHub rollback record: "
                    f"{github_result.deployment_id}"
                )
            except Exception as exc:
                logger.warning(
                    f"[CompositeProvider] GitHub rollback record failed (non-fatal): {exc}"
                )

        result = k8s_result or github_result
        if result is None:
            raise RuntimeError(
                "CompositeProvider.rollback(): both GitHub and K8s failed"
            )
        return result

    # ── status & logs — prefer K8s (real pod state) ──────────────────────────

    async def get_deployment_status(self, deployment_id: str) -> Deployment:
        if self._k8s and "K8S" in deployment_id:
            return await self._k8s.get_deployment_status(deployment_id)
        if self._github:
            return await self._github.get_deployment_status(deployment_id)
        return await self._k8s.get_deployment_status(deployment_id)

    async def collect_logs(self, run_id: str, repo: str) -> list[str]:
        """Collect logs from both providers and merge."""
        logs = []

        if self._github and not run_id.startswith("k8s-"):
            try:
                logs += await self._github.collect_logs(run_id, repo)
            except Exception as exc:
                logger.warning(f"[CompositeProvider] GitHub logs failed: {exc}")

        if self._k8s:
            try:
                k8s_logs = await self._k8s.collect_logs(run_id, repo)
                if k8s_logs:
                    logs += ["--- Kubernetes pod logs ---"] + k8s_logs
            except Exception as exc:
                logger.warning(f"[CompositeProvider] K8s logs failed: {exc}")

        return logs or ["No logs available from any provider"]

    async def health_check(self) -> bool:
        github_ok = True
        k8s_ok    = True

        if self._github:
            try:
                github_ok = await self._github.health_check()
            except Exception:
                github_ok = False

        if self._k8s:
            try:
                k8s_ok = await self._k8s.health_check()
            except Exception:
                k8s_ok = False

        logger.info(
            f"[CompositeProvider] Health — "
            f"github={github_ok} k8s={k8s_ok}"
        )
        # Healthy if at least one provider is reachable
        return github_ok or k8s_ok