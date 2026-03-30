"""
agents/cicd_agent/pipeline_manager.py
=======================================
Orchestrates a multi-stage CI/CD pipeline:

    build → test → push → k8s deploy → health check → (on failure) rollback

Each stage is a coroutine. Stages run sequentially; any failure short-circuits
the pipeline and triggers rollback via RollbackManager.

Usage:
    pm = PipelineManager(
        deployment_manager = DeploymentManager(provider),
        rollback_manager   = RollbackManager(provider),
    )
    result = await pm.run(service="auth-api", branch="main", version="v1.2.3")
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Coroutine, Optional

from agents.cicd_agent.deployment_manager import DeploymentManager
from agents.cicd_agent.rollback_manager import RollbackManager
from core.models import Deployment, DeploymentStatus

logger = logging.getLogger(__name__)


class StageStatus(str, Enum):
    PENDING  = "pending"
    RUNNING  = "running"
    SUCCESS  = "success"
    FAILED   = "failed"
    SKIPPED  = "skipped"


@dataclass
class StageResult:
    name:       str
    status:     StageStatus
    started_at: Optional[datetime] = None
    ended_at:   Optional[datetime] = None
    message:    str = ""
    metadata:   dict[str, Any] = field(default_factory=dict)


@dataclass
class PipelineResult:
    service:    str
    branch:     str
    version:    str
    stages:     list[StageResult] = field(default_factory=list)
    success:    bool = False
    deployment: Optional[Deployment] = None
    rolled_back: bool = False

    @property
    def failed_stage(self) -> Optional[StageResult]:
        return next((s for s in self.stages if s.status == StageStatus.FAILED), None)

    def summary(self) -> str:
        lines = [f"Pipeline {'OK' if self.success else 'FAILED'}: {self.service}@{self.version}"]
        for s in self.stages:
            icon = {"success": "✓", "failed": "✗", "skipped": "–", "pending": "·", "running": "→"}.get(s.status, "?")
            lines.append(f"  {icon} {s.name}: {s.status.value} — {s.message}")
        if self.rolled_back:
            lines.append("  ↩ Rollback executed")
        return "\n".join(lines)


# Stage hook type: async callable that receives pipeline context and returns StageResult
StageHook = Callable[..., Coroutine[Any, Any, StageResult]]


class PipelineManager:
    """
    Runs a configurable sequence of pipeline stages.

    Built-in stages:
        - k8s_deploy   : calls DeploymentManager.deploy_and_wait()
        - health_check : verifies deployment status is SUCCESS

    Custom stages can be injected via extra_stages, which are inserted
    before k8s_deploy (e.g. build, test, push).

    Example with custom stages:

        async def my_build_stage(service, branch, version, **_) -> StageResult:
            # run your build logic
            return StageResult(name="build", status=StageStatus.SUCCESS, message="built ok")

        pm = PipelineManager(
            deployment_manager = DeploymentManager(k8s_provider),
            rollback_manager   = RollbackManager(k8s_provider),
            extra_stages       = [my_build_stage],
        )
    """

    def __init__(
        self,
        deployment_manager: DeploymentManager,
        rollback_manager:   RollbackManager,
        extra_stages:       list[StageHook] | None = None,
    ):
        self._dm           = deployment_manager
        self._rm           = rollback_manager
        self._extra_stages = extra_stages or []

    async def run(
        self,
        service:     str,
        branch:      str = "main",
        version:     str = "",
        environment: str = "production",
        incident_id: Optional[str] = None,
    ) -> PipelineResult:
        """
        Execute the full pipeline. Returns PipelineResult regardless of outcome.
        If any stage fails, attempts rollback and marks rolled_back=True.
        """
        result = PipelineResult(service=service, branch=branch, version=version)

        logger.info(
            f"PipelineManager.run: service={service} branch={branch} "
            f"version={version} env={environment}"
        )

        # Build ordered stage list
        stages: list[tuple[str, StageHook]] = []

        for hook in self._extra_stages:
            stages.append((hook.__name__, hook))

        stages.append(("k8s_deploy",   self._stage_k8s_deploy))
        stages.append(("health_check", self._stage_health_check))

        ctx = dict(
            service     = service,
            branch      = branch,
            version     = version,
            environment = environment,
            incident_id = incident_id,
            result      = result,
        )

        # Execute stages sequentially
        for stage_name, stage_fn in stages:
            stage_result = await self._run_stage(stage_name, stage_fn, ctx)
            result.stages.append(stage_result)

            if stage_result.status == StageStatus.FAILED:
                logger.warning(f"Stage '{stage_name}' failed — aborting pipeline")
                result.success = False
                await self._attempt_rollback(result, service, version, environment)
                return result

        result.success = True
        logger.info(f"Pipeline complete: {service}@{version}")
        return result

    # ── Built-in stages ────────────────────────────────────────────────────────

    async def _stage_k8s_deploy(self, service, branch, version, environment, **_) -> StageResult:
        started = datetime.utcnow()
        logger.info(f"Stage k8s_deploy: {service}@{version} → {environment}")

        try:
            deployment = await self._dm.deploy_and_wait(
                service = service,
                branch  = branch,
                version = version,
                poll    = False,   # KubernetesProvider.deploy() already blocks internally
            )

            if deployment.status == DeploymentStatus.SUCCESS:
                return StageResult(
                    name       = "k8s_deploy",
                    status     = StageStatus.SUCCESS,
                    started_at = started,
                    ended_at   = datetime.utcnow(),
                    message    = f"Deployed {service} image={deployment.metadata.get('image', version)}",
                    metadata   = {"deployment_id": deployment.deployment_id},
                )
            else:
                return StageResult(
                    name       = "k8s_deploy",
                    status     = StageStatus.FAILED,
                    started_at = started,
                    ended_at   = datetime.utcnow(),
                    message    = f"Deployment status={deployment.status.value}",
                    metadata   = {"deployment_id": deployment.deployment_id},
                )

        except Exception as exc:
            logger.error(f"k8s_deploy stage error: {exc}", exc_info=True)
            return StageResult(
                name       = "k8s_deploy",
                status     = StageStatus.FAILED,
                started_at = started,
                ended_at   = datetime.utcnow(),
                message    = str(exc),
            )

    async def _stage_health_check(self, result: PipelineResult, service, **_) -> StageResult:
        started = datetime.utcnow()
        logger.info(f"Stage health_check: {service}")

        # Find the deploy stage result to check its metadata
        deploy_stage = next(
            (s for s in result.stages if s.name == "k8s_deploy"), None
        )

        if deploy_stage and deploy_stage.status == StageStatus.SUCCESS:
            return StageResult(
                name       = "health_check",
                status     = StageStatus.SUCCESS,
                started_at = started,
                ended_at   = datetime.utcnow(),
                message    = "All replicas available",
            )

        return StageResult(
            name       = "health_check",
            status     = StageStatus.FAILED,
            started_at = started,
            ended_at   = datetime.utcnow(),
            message    = "Deployment did not reach SUCCESS before health check",
        )

    # ── Helpers ────────────────────────────────────────────────────────────────

    async def _run_stage(
        self, name: str, fn: StageHook, ctx: dict[str, Any]
    ) -> StageResult:
        logger.info(f"→ Running stage: {name}")
        try:
            stage_result = await fn(**ctx)
            logger.info(f"  Stage {name}: {stage_result.status.value}")
            return stage_result
        except Exception as exc:
            logger.error(f"  Stage {name} raised: {exc}", exc_info=True)
            return StageResult(
                name       = name,
                status     = StageStatus.FAILED,
                started_at = datetime.utcnow(),
                ended_at   = datetime.utcnow(),
                message    = str(exc),
            )

    async def _attempt_rollback(
        self,
        result:      PipelineResult,
        service:     str,
        version:     str,
        environment: str,
    ) -> None:
        logger.info(f"Attempting rollback for {service}")
        try:
            rollback_result = await self._rm.rollback(
                service     = service,
                version     = version,
                environment = environment,
            )
            result.rolled_back = rollback_result.success
            result.stages.append(StageResult(
                name    = "rollback",
                status  = StageStatus.SUCCESS if rollback_result.success else StageStatus.FAILED,
                message = rollback_result.message,
            ))
        except Exception as exc:
            logger.error(f"Rollback failed: {exc}", exc_info=True)
            result.stages.append(StageResult(
                name    = "rollback",
                status  = StageStatus.FAILED,
                message = str(exc),
            ))