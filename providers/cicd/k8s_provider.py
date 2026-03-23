"""
providers/cicd/k8s_provider.py
--------------------------------
Kubernetes deployment provider for the CI/CD Agent.

What it does
------------
- deploy()        → kubectl set image  (rolling update)
- rollback()      → kubectl rollout undo
- get_deployment_status() → kubectl rollout status
- health_check()  → kubectl get nodes

Uses kubectl via subprocess — inherits whatever kubeconfig
is active in the environment (local, in-cluster, or explicit
KUBECONFIG env var).

No extra dependencies beyond kubectl being in PATH.

Construction
------------
    provider = KubernetesProvider(
        namespace  = "production",
        kubeconfig = "/path/to/kubeconfig",   # optional
        context    = "my-cluster",            # optional
        timeout    = 120,
    )

All methods return the same types as BaseCICDProvider so the
CICDAgent can swap GitHub ↔ K8s ↔ both at runtime.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
from datetime import datetime
from typing import Any, Optional

from core.models import Deployment, DeploymentStatus
from providers.cicd.base_provider import (
    BaseCICDProvider, PipelineRun, RollbackResult,
)

logger = logging.getLogger(__name__)


# ── helpers ───────────────────────────────────────────────────────────────────

def _kubectl_available() -> bool:
    return shutil.which("kubectl") is not None


async def _run(
    cmd: list[str],
    timeout: int = 60,
) -> tuple[int, str, str]:
    """Run a kubectl command asynchronously. Returns (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise RuntimeError(
            f"kubectl timed out after {timeout}s: {' '.join(cmd)}"
        )
    return proc.returncode, stdout.decode().strip(), stderr.decode().strip()


# ── provider ──────────────────────────────────────────────────────────────────

class KubernetesProvider(BaseCICDProvider):
    """
    Kubernetes deployment provider.

    Executes kubectl commands to manage rolling deployments,
    rollbacks, and deployment health checks.

    All methods are async and compatible with the CICDAgent interface.
    """

    def __init__(
        self,
        namespace:  str            = "default",
        kubeconfig: Optional[str]  = None,
        context:    Optional[str]  = None,
        timeout:    int            = 120,
    ):
        self._namespace  = namespace
        self._kubeconfig = kubeconfig or os.getenv("KUBECONFIG", "")
        self._context    = context
        self._timeout    = timeout

        if not _kubectl_available():
            logger.warning(
                "[KubernetesProvider] kubectl not found in PATH — "
                "all operations will fail until kubectl is installed"
            )
        logger.info(
            f"[KubernetesProvider] Initialized — "
            f"namespace={namespace} context={context or 'default'}"
        )

    @property
    def name(self) -> str:
        return "kubernetes"

    # ── kubectl base command ──────────────────────────────────────────────────

    def _base(self) -> list[str]:
        """Build the base kubectl command with namespace and optional context/kubeconfig."""
        cmd = ["kubectl", "--namespace", self._namespace]
        if self._kubeconfig:
            cmd += ["--kubeconfig", self._kubeconfig]
        if self._context:
            cmd += ["--context", self._context]
        return cmd

    # ── BaseCICDProvider interface ────────────────────────────────────────────

    async def trigger_pipeline(
        self,
        repo:     str,
        branch:   str = "main",
        workflow: str = "",
        inputs:   dict | None = None,
    ) -> PipelineRun:
        """
        K8s doesn't have pipelines — this triggers a rollout restart
        of the deployment named after `repo` (or the workflow arg).
        """
        deployment_name = workflow or repo.split("/")[-1]
        cmd = self._base() + [
            "rollout", "restart",
            f"deployment/{deployment_name}",
        ]
        rc, stdout, stderr = await _run(cmd, timeout=self._timeout)

        if rc != 0:
            raise RuntimeError(
                f"kubectl rollout restart failed [{rc}]: {stderr or stdout}"
            )

        run_id = f"k8s-restart-{deployment_name}-{datetime.utcnow().strftime('%H%M%S')}"
        logger.info(f"[KubernetesProvider] Rollout restart triggered: {deployment_name}")

        return PipelineRun(
            id         = run_id,
            repo       = repo,
            branch     = branch,
            workflow   = deployment_name,
            status     = "running",
            url        = f"kubectl rollout status deployment/{deployment_name} -n {self._namespace}",
            started_at = datetime.utcnow(),
        )

    async def get_pipeline_status(self, run_id: str, repo: str) -> PipelineRun:
        """
        Check rollout status for the deployment extracted from run_id.
        run_id format: k8s-restart-<deployment>-<time> or k8s-deploy-<deployment>-<time>
        """
        parts = run_id.split("-")
        deployment_name = parts[2] if len(parts) >= 3 else repo.split("/")[-1]

        cmd = self._base() + [
            "rollout", "status",
            f"deployment/{deployment_name}",
            "--timeout=10s",
        ]
        rc, stdout, stderr = await _run(cmd, timeout=30)

        if rc == 0 and "successfully rolled out" in stdout.lower():
            status = "success"
        elif rc != 0 and "timed out" in stderr.lower():
            status = "running"
        elif rc != 0:
            status = "failed"
        else:
            status = "running"

        return PipelineRun(
            id          = run_id,
            repo        = repo,
            branch      = "main",
            workflow    = deployment_name,
            status      = status,
            url         = f"kubectl rollout status deployment/{deployment_name}",
            finished_at = datetime.utcnow() if status != "running" else None,
        )

    async def deploy(
        self,
        service:     str,
        branch:      str = "main",
        environment: str = "production",
        version:     str = "",
    ) -> Deployment:
        """
        Rolling update — sets the container image on the deployment.

        service   : deployment name (e.g. "auth-api" or "org/auth-api")
        version   : new image tag (e.g. "v2.3.1" or full image URL)
        branch    : used as image tag when version is empty

        Example command:
            kubectl set image deployment/auth-api \
                auth-api=ghcr.io/org/auth-api:v2.3.1 \
                -n production
        """
        deployment_name = service.split("/")[-1]
        image_tag       = version or branch

        # Detect if version is a full image URL or just a tag
        if ":" in image_tag or "/" in image_tag:
            image = image_tag
        else:
            image = f"{deployment_name}:{image_tag}"

        dep_id = f"DEP-K8S-{deployment_name.upper()[:8]}-{datetime.utcnow().strftime('%H%M%S')}"

        cmd = self._base() + [
            "set", "image",
            f"deployment/{deployment_name}",
            f"{deployment_name}={image}",
        ]

        logger.info(
            f"[KubernetesProvider] Deploying {deployment_name} → {image} "
            f"(namespace={self._namespace})"
        )
        rc, stdout, stderr = await _run(cmd, timeout=self._timeout)

        if rc != 0:
            raise RuntimeError(
                f"kubectl set image failed [{rc}]: {stderr or stdout}"
            )

        logger.info(f"[KubernetesProvider] Deployment started: {dep_id}")

        return Deployment(
            service       = service,
            branch        = branch,
            version       = image_tag,
            deployment_id = dep_id,
            status        = DeploymentStatus.RUNNING,
            pipeline_url  = (
                f"kubectl rollout status deployment/{deployment_name} "
                f"-n {self._namespace}"
            ),
            started_at    = datetime.utcnow(),
            metadata      = {
                "provider":    "kubernetes",
                "namespace":   self._namespace,
                "deployment":  deployment_name,
                "image":       image,
                "environment": environment,
            },
        )

    async def get_deployment_status(self, deployment_id: str) -> Deployment:
        """
        Parse deployment_id to extract the deployment name and check rollout status.
        deployment_id format: DEP-K8S-<NAME>-<TIME>
        """
        parts = deployment_id.split("-")
        deployment_name = parts[2].lower() if len(parts) >= 3 else "unknown"

        cmd = self._base() + [
            "rollout", "status",
            f"deployment/{deployment_name}",
            "--timeout=5s",
        ]
        rc, stdout, _ = await _run(cmd, timeout=15)

        if rc == 0 and "successfully rolled out" in stdout.lower():
            status = DeploymentStatus.SUCCESS
        elif rc != 0:
            status = DeploymentStatus.FAILED
        else:
            status = DeploymentStatus.RUNNING

        return Deployment(
            service       = deployment_name,
            branch        = "main",
            version       = "",
            deployment_id = deployment_id,
            status        = status,
            finished_at   = datetime.utcnow() if status != DeploymentStatus.RUNNING else None,
        )

    async def rollback(
        self,
        service:     str,
        version:     str,
        environment: str = "production",
    ) -> RollbackResult:
        """
        kubectl rollout undo — rolls back to the previous revision.
        If version looks like a revision number (e.g. "2"), uses --to-revision.
        """
        deployment_name = service.split("/")[-1]

        cmd = self._base() + ["rollout", "undo", f"deployment/{deployment_name}"]

        # If version is a revision number, use --to-revision
        if version.isdigit():
            cmd += [f"--to-revision={version}"]
            rolled_back_to = f"revision {version}"
        else:
            rolled_back_to = "previous revision"

        logger.info(
            f"[KubernetesProvider] Rolling back {deployment_name} "
            f"to {rolled_back_to} (namespace={self._namespace})"
        )
        rc, stdout, stderr = await _run(cmd, timeout=self._timeout)

        if rc != 0:
            raise RuntimeError(
                f"kubectl rollout undo failed [{rc}]: {stderr or stdout}"
            )

        dep_id = (
            f"DEP-K8S-{deployment_name.upper()[:8]}-"
            f"RB-{datetime.utcnow().strftime('%H%M%S')}"
        )
        logger.info(f"[KubernetesProvider] Rollback complete: {deployment_name}")

        return RollbackResult(
            deployment_id  = dep_id,
            service        = service,
            rolled_back_to = rolled_back_to,
            status         = DeploymentStatus.ROLLED_BACK,
            message        = f"Rolled back {deployment_name} to {rolled_back_to}",
        )

    async def collect_logs(self, run_id: str, repo: str) -> list[str]:
        """Fetch recent pod logs for the deployment."""
        deployment_name = repo.split("/")[-1]

        cmd = self._base() + [
            "logs",
            f"deployment/{deployment_name}",
            "--tail=50",
            "--prefix",
        ]
        rc, stdout, stderr = await _run(cmd, timeout=30)

        if rc != 0:
            return [f"[kubectl logs error] {stderr or stdout}"]

        lines = [line for line in stdout.splitlines() if line.strip()]
        logger.info(
            f"[KubernetesProvider] Collected {len(lines)} log lines "
            f"from {deployment_name}"
        )
        return lines

    async def health_check(self) -> bool:
        """Verify kubectl can reach the cluster by listing nodes."""
        cmd = self._base()[:-2] + ["get", "nodes", "--no-headers"]
        try:
            rc, stdout, _ = await _run(cmd, timeout=10)
            return rc == 0 and bool(stdout)
        except Exception:
            return False