"""
providers/cicd/base_provider.py
================================
Abstract contract every CI/CD provider must satisfy.

Key design decision:
    deploy() and rollback() return core.models.Deployment directly.
    This means results go straight into StateManager and ContextManager
    without any translation layer.

    PipelineRun is a lightweight supplementary dataclass for pipeline
    operations that don't map to a full Deployment (e.g. lint, test runs).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from core.models import Deployment, DeploymentStatus


# ── PipelineRun ───────────────────────────────────────────────────────────────
# Not in core/models.py — pipeline runs are CI concerns, not system state.

@dataclass
class PipelineRun:
    """
    Result of triggering a CI pipeline (GitHub Actions workflow run,
    GitLab CI pipeline, Jenkins build, etc.).

    status values: "pending" | "running" | "success" | "failed" | "cancelled"
    """
    id:          str
    repo:        str
    branch:      str
    workflow:    str
    status:      str              = "pending"
    url:         str              = ""
    started_at:  Optional[datetime] = None
    finished_at: Optional[datetime] = None
    logs:        list[str]        = field(default_factory=list)
    raw:         dict[str, Any]   = field(default_factory=dict)

    def __str__(self):
        return f"PipelineRun(id={self.id}, status={self.status}, repo={self.repo})"


# ── RollbackResult ────────────────────────────────────────────────────────────

@dataclass
class RollbackResult:
    """
    Returned by provider.rollback().
    The CICDAgent uses this to build a core.models.Deployment record
    with status=ROLLED_BACK before storing it in StateManager.
    """
    deployment_id:  str
    service:        str
    rolled_back_to: str
    status:         DeploymentStatus
    message:        str           = ""
    raw:            dict[str, Any] = field(default_factory=dict)

    def __str__(self):
        return (
            f"RollbackResult(service={self.service}, "
            f"version={self.rolled_back_to}, status={self.status.value})"
        )


# ── BaseCICDProvider ──────────────────────────────────────────────────────────

class BaseCICDProvider(ABC):
    """
    Abstract base for GitHub, GitLab, Jenkins, and ArgoCD providers.

    Contract:
        - deploy()   returns core.models.Deployment
        - rollback() returns RollbackResult
        - All other methods return PipelineRun or list[str]

    This ensures the CICDAgent never needs to know which provider
    is active — it always hands core.models.Deployment to the
    StateManager and ContextManager.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Identifier string: 'github', 'gitlab', 'jenkins', etc."""
        ...

    @abstractmethod
    async def trigger_pipeline(
        self,
        repo:     str,
        branch:   str = "main",
        workflow: str = "",
        inputs:   dict[str, Any] | None = None,
    ) -> PipelineRun:
        """Trigger a CI pipeline and return the run immediately (don't wait)."""
        ...

    @abstractmethod
    async def get_pipeline_status(
        self,
        run_id: str,
        repo:   str,
    ) -> PipelineRun:
        """Poll the current status of a pipeline run."""
        ...

    @abstractmethod
    async def deploy(
        self,
        service:     str,
        branch:      str = "main",
        environment: str = "production",
        version:     str = "",
    ) -> Deployment:
        """
        Deploy a service and return a core.models.Deployment.
        The status field should be RUNNING or SUCCESS depending on
        whether the provider is synchronous or async.
        """
        ...

    @abstractmethod
    async def get_deployment_status(
        self,
        deployment_id: str,
    ) -> Deployment:
        """Fetch the current status of a deployment by ID."""
        ...

    @abstractmethod
    async def rollback(
        self,
        service:     str,
        version:     str,
        environment: str = "production",
    ) -> RollbackResult:
        """Roll back a service to a previous version/tag/SHA."""
        ...

    @abstractmethod
    async def collect_logs(
        self,
        run_id: str,
        repo:   str,
    ) -> list[str]:
        """Fetch log lines from a pipeline run."""
        ...

    async def health_check(self) -> bool:
        """Optional: verify provider is reachable. Default True."""
        return True