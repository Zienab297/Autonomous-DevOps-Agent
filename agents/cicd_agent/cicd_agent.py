"""
agents/cicd_agent/cicd_agent.py
==========================
The CI/CD Agent — a first-class citizen of the core architecture.

Inherits BaseAgent, so the core framework fully manages its lifecycle:
    - start()     → registers in AgentRegistry, calls _setup(), starts heartbeat
    - _setup()    → subscribes to EventBus events, sets own status in StateManager
    - stop()      → unsubscribes from bus, unregisters from AgentRegistry
    - _teardown() → closes provider HTTP session

EventBus integration:
    Listens for:
        EventType.DEPLOYMENT_STARTED  → runs _handle_deploy()
        EventType.ROLLBACK_TRIGGERED  → runs _handle_rollback()

    Publishes:
        EventType.DEPLOYMENT_STARTED  → when deploy() is called directly
        EventType.DEPLOYMENT_COMPLETE → after every deploy/rollback
        EventType.ROLLBACK_TRIGGERED  → when rollback() is called directly

    Recursion guard:
        deploy() and rollback() publish events that they also listen to.
        To avoid infinite loops, handle_event() calls internal _handle_*
        methods that go straight to the provider — bypassing publish.
        Only direct API calls (deploy(), rollback()) publish to the bus.

StateManager integration:
    - Stores every Deployment via state_manager.add_deployment()
    - Updates own agent status via state_manager.set_agent_status()

ContextManager integration:
    - Attaches every Deployment to its IncidentContext when incident_id given
    - context_manager.add_deployment(incident_id, deployment)
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

from core.base_agent import BaseAgent, AgentEvent
from core.event_bus  import Event, EventType
from core.models import AgentStatus, Deployment, DeploymentStatus
from core.state_manager import StateManager
from core.context_manager import ContextManager
from providers.cicd.base_provider import BaseCICDProvider, PipelineRun, RollbackResult
from agents.cicd_agent.deployment_manager import DeploymentManager
from agents.cicd_agent.rollback_manager import RollbackManager
from agents.cicd_agent.pipeline_manager import PipelineManager, PipelineResult

logger = logging.getLogger(__name__)


class CICDAgent(BaseAgent):
    """
    Manages all CI/CD operations: deploy, rollback, pipeline triggers.

    Construction:
        agent = CICDAgent(
            provider    = GitHubProvider(),
            event_bus   = orchestrator.event_bus,
            registry    = orchestrator.registry,
            state       = orchestrator.state_manager,
            ctx_manager = orchestrator.context_manager,
        )

    Lifecycle (handled by BaseAgent):
        await agent.start()   # registers, subscribes, marks RUNNING
        await agent.stop()    # unsubscribes, closes session, marks STOPPED

    Direct calls:
        dep    = await agent.deploy("my-org/auth-api", branch="main")
        result = await agent.rollback("my-org/auth-api", version="v1.0.0")
        run    = await agent.trigger_pipeline("my-org/auth-api")
    """

    AGENT_NAME = "cicd_agent"

    def __init__(
        self,
        provider:    BaseCICDProvider,
        event_bus=None,
        registry=None,
        state:       Optional[StateManager]   = None,
        ctx_manager: Optional[ContextManager] = None,
        heartbeat_interval: float = 30.0,
    ):
        super().__init__(
            name               = self.AGENT_NAME,
            event_bus          = event_bus,
            registry           = registry,
            heartbeat_interval = heartbeat_interval,
        )
        self.provider     = provider
        self._state_mgr   = state
        self._ctx_manager = ctx_manager

        # Higher-level managers — usable directly or via run_pipeline()
        self._deployment_mgr = DeploymentManager(provider)
        self._rollback_mgr   = RollbackManager(provider)
        self._pipeline_mgr   = PipelineManager(
            deployment_manager = self._deployment_mgr,
            rollback_manager   = self._rollback_mgr,
        )

        self.logger.info(f"CICDAgent created — provider={provider.name}")

    # ─────────────────────────────────────────────────────────────────────────
    # BaseAgent lifecycle hooks
    # ─────────────────────────────────────────────────────────────────────────

    async def _setup(self) -> None:
        """
        Called by BaseAgent.start() before state becomes RUNNING.
        - Subscribe to bus events
        - Register own status in StateManager
        """
        self.subscribe(EventType.DEPLOYMENT_STARTED, self.handle_event)
        self.subscribe(EventType.ROLLBACK_TRIGGERED,  self.handle_event)

        if self._state_mgr:
            self._state_mgr.set_agent_status(self.AGENT_NAME, AgentStatus.RUNNING)

        self.logger.info(
            "CICDAgent subscribed to DEPLOYMENT_STARTED, ROLLBACK_TRIGGERED"
        )

    async def _teardown(self) -> None:
        """
        Called by BaseAgent.stop() before state becomes STOPPED.
        Closes the provider HTTP session.
        """
        if hasattr(self.provider, "close"):
            await self.provider.close()
        if self._state_mgr:
            self._state_mgr.set_agent_status(self.AGENT_NAME, AgentStatus.STOPPED)
        self.logger.info("CICDAgent teardown complete")

    # ─────────────────────────────────────────────────────────────────────────
    # handle_event — receives events from EventBus
    # ─────────────────────────────────────────────────────────────────────────

    async def handle_event(self, event: Event) -> Any:
        """
        Dispatches bus events to internal handlers.

        Uses _handle_deploy / _handle_rollback instead of deploy / rollback
        to avoid re-publishing the same event type and causing recursion.
        """
        self.logger.info(
            f"CICDAgent received: type={event.type} "
            f"source={event.source} incident={event.incident_id}"
        )

        if self._state_mgr:
            self._state_mgr.set_agent_status(self.AGENT_NAME, AgentStatus.RUNNING)

        try:
            if event.type == EventType.DEPLOYMENT_STARTED:
                await self._handle_deploy(
                    service     = event.data.get("service", ""),
                    branch      = event.data.get("branch", "main"),
                    environment = event.data.get("environment", "production"),
                    version     = event.data.get("version", ""),
                    incident_id = event.incident_id,
                )

            elif event.type == EventType.ROLLBACK_TRIGGERED:
                await self._handle_rollback(
                    service     = event.data.get("service", ""),
                    version     = event.data.get("version", ""),
                    environment = event.data.get("environment", "production"),
                    incident_id = event.incident_id,
                )
        finally:
            if self._state_mgr:
                self._state_mgr.set_agent_status(self.AGENT_NAME, AgentStatus.IDLE)

    # ─────────────────────────────────────────────────────────────────────────
    # Internal handlers — called from handle_event, no bus publishing
    # ─────────────────────────────────────────────────────────────────────────

    async def _handle_deploy(
        self,
        service:     str,
        branch:      str,
        environment: str,
        version:     str,
        incident_id: Optional[str],
    ) -> Deployment:
        """
        Core deploy logic. Called from handle_event (bus-triggered).
        Does NOT publish DEPLOYMENT_STARTED (already on the bus).
        Publishes DEPLOYMENT_COMPLETE on finish.
        """
        self.logger.info(
            f"_handle_deploy: service={service} branch={branch} "
            f"env={environment} incident={incident_id}"
        )
        try:
            deployment = await self.provider.deploy(
                service=service, branch=branch,
                environment=environment, version=version,
            )
            await self._persist_deployment(deployment, incident_id)
            await self._publish_complete(deployment, incident_id)
            return deployment

        except Exception as exc:
            self.logger.error(f"_handle_deploy failed: {exc}", exc_info=True)
            await self._publish_failed(service, incident_id, str(exc))
            raise

    async def _handle_rollback(
        self,
        service:     str,
        version:     str,
        environment: str,
        incident_id: Optional[str],
    ) -> RollbackResult:
        """
        Core rollback logic. Called from handle_event (bus-triggered).
        Does NOT publish ROLLBACK_TRIGGERED (already on the bus).
        Publishes DEPLOYMENT_COMPLETE on finish.
        """
        self.logger.info(
            f"_handle_rollback: service={service} version={version} "
            f"env={environment} incident={incident_id}"
        )
        try:
            result = await self.provider.rollback(
                service=service, version=version, environment=environment,
            )
            rolled_dep = self._build_rollback_deployment(
                result, service, version, environment
            )
            await self._persist_deployment(rolled_dep, incident_id)
            await self._publish_rollback_complete(result, incident_id, version, environment)
            return result

        except Exception as exc:
            self.logger.error(f"_handle_rollback failed: {exc}", exc_info=True)
            raise

    # ─────────────────────────────────────────────────────────────────────────
    # Public API — direct calls from Orchestrator or other agents
    # ─────────────────────────────────────────────────────────────────────────

    async def deploy(
        self,
        service:     str,
        branch:      str = "main",
        environment: str = "production",
        version:     str = "",
        incident_id: Optional[str] = None,
    ) -> Deployment:
        """
        Deploy a service. Publishes DEPLOYMENT_STARTED then calls provider.

        Flow:
            1. Publish DEPLOYMENT_STARTED to bus (notifies Monitoring Agent etc.)
            2. Call provider.deploy()
            3. Store Deployment in StateManager
            4. Attach to IncidentContext if incident_id given
            5. Publish DEPLOYMENT_COMPLETE

        Returns: core.models.Deployment
        """
        self.logger.info(
            f"deploy: service={service} branch={branch} "
            f"env={environment} incident={incident_id}"
        )

        await self.publish(Event(
            type        = EventType.DEPLOYMENT_STARTED,
            source      = self.name,
            incident_id = incident_id,
            data        = {
                "service":     service,
                "branch":      branch,
                "environment": environment,
                "version":     version,
            },
        ))

        try:
            deployment = await self.provider.deploy(
                service=service, branch=branch,
                environment=environment, version=version,
            )
            await self._persist_deployment(deployment, incident_id)
            await self._publish_complete(deployment, incident_id)
            self.logger.info(
                f"deploy complete: {deployment.deployment_id} "
                f"status={deployment.status.value}"
            )
            return deployment

        except Exception as exc:
            self.logger.error(f"deploy failed: {exc}", exc_info=True)
            await self._publish_failed(service, incident_id, str(exc))
            raise

    async def rollback(
        self,
        service:     str,
        version:     str,
        environment: str = "production",
        incident_id: Optional[str] = None,
    ) -> RollbackResult:
        """
        Roll back a service to a previous version.

        Flow:
            1. Publish ROLLBACK_TRIGGERED to bus
            2. Call provider.rollback()
            3. Store ROLLED_BACK Deployment in StateManager
            4. Attach to IncidentContext if incident_id given
            5. Publish DEPLOYMENT_COMPLETE with rolled_back status

        Returns: RollbackResult
        """
        self.logger.info(
            f"rollback: service={service} version={version} "
            f"env={environment} incident={incident_id}"
        )

        await self.publish(Event(
            type        = EventType.ROLLBACK_TRIGGERED,
            source      = self.name,
            incident_id = incident_id,
            data        = {
                "service":     service,
                "version":     version,
                "environment": environment,
            },
        ))

        try:
            result = await self.provider.rollback(
                service=service, version=version, environment=environment,
            )
            rolled_dep = self._build_rollback_deployment(
                result, service, version, environment
            )
            await self._persist_deployment(rolled_dep, incident_id)
            await self._publish_rollback_complete(result, incident_id, version, environment)
            self.logger.info(f"rollback complete: {service} → {version}")
            return result

        except Exception as exc:
            self.logger.error(f"rollback failed: {exc}", exc_info=True)
            raise

    async def trigger_pipeline(
        self,
        repo:        str,
        branch:      str = "main",
        workflow:    str = "deploy.yml",
        inputs:      dict[str, Any] | None = None,
        incident_id: Optional[str] = None,
    ) -> PipelineRun:
        """
        Trigger a CI pipeline without creating a Deployment record.
        Publishes DEPLOYMENT_STARTED with the run_id for tracking.
        """
        self.logger.info(
            f"trigger_pipeline: repo={repo} branch={branch} workflow={workflow}"
        )
        run = await self.provider.trigger_pipeline(
            repo=repo, branch=branch, workflow=workflow, inputs=inputs,
        )
        await self.publish(Event(
            type        = EventType.DEPLOYMENT_STARTED,
            source      = self.name,
            incident_id = incident_id,
            data        = {
                "service":  repo,
                "branch":   branch,
                "run_id":   run.id,
                "workflow": run.workflow,
                "url":      run.url,
                "status":   run.status,
            },
        ))
        self.logger.info(
            f"trigger_pipeline: run_id={run.id} status={run.status}"
        )
        return run

    async def get_pipeline_status(self, run_id: str, repo: str) -> PipelineRun:
        """Poll current status of a pipeline run."""
        run = await self.provider.get_pipeline_status(run_id, repo)
        self.logger.debug(f"pipeline {run_id} status={run.status}")
        return run

    async def collect_deployment_logs(self, run_id: str, repo: str) -> list[str]:
        """
        Fetch log lines from a pipeline run.
        Used by the KnowledgeAgent during incident investigation.
        """
        return await self.provider.collect_logs(run_id, repo)

    async def run_pipeline(
        self,
        service:     str,
        branch:      str = "main",
        version:     str = "",
        environment: str = "production",
        incident_id: Optional[str] = None,
    ) -> PipelineResult:
        """
        Run the full CI/CD pipeline (build stages → k8s deploy → health check).
        Publishes DEPLOYMENT_STARTED before the pipeline and DEPLOYMENT_COMPLETE
        after it, whether it succeeds or fails.

        For the Kubernetes provider this is the preferred entry point over
        deploy() — it adds health checking and automatic rollback on failure.
        """
        self.logger.info(
            f"run_pipeline: service={service} branch={branch} "
            f"version={version} env={environment}"
        )

        await self.publish(Event(
            type        = EventType.DEPLOYMENT_STARTED,
            source      = self.name,
            incident_id = incident_id,
            data        = {"service": service, "branch": branch,
                           "environment": environment, "version": version},
        ))

        result = await self._pipeline_mgr.run(
            service     = service,
            branch      = branch,
            version     = version,
            environment = environment,
            incident_id = incident_id,
        )

        # Persist the deployment record if k8s_deploy stage produced one
        if result.deployment:
            await self._persist_deployment(result.deployment, incident_id)

        await self.publish(Event(
            type        = EventType.DEPLOYMENT_COMPLETE,
            source      = self.name,
            incident_id = incident_id,
            data        = {
                "service":      service,
                "version":      version,
                "environment":  environment,
                "success":      result.success,
                "rolled_back":  result.rolled_back,
                "failed_stage": result.failed_stage.name if result.failed_stage else None,
            },
        ))

        self.logger.info(result.summary())
        return result

    async def health_check(self) -> dict[str, Any]:
        """Check provider connectivity. Returns dict for Orchestrator.summary()."""
        healthy = await self.provider.health_check()
        return {
            "agent":    self.name,
            "agent_id": self.agent_id,
            "state":    self._state.value,
            "provider": self.provider.name,
            "healthy":  healthy,
        }

    # ─────────────────────────────────────────────────────────────────────────
    # Private helpers
    # ─────────────────────────────────────────────────────────────────────────

    async def _persist_deployment(
        self,
        deployment:  Deployment,
        incident_id: Optional[str],
    ) -> None:
        """Store deployment in StateManager and attach to IncidentContext."""
        if self._state_mgr:
            self._state_mgr.add_deployment(deployment)
        if incident_id and self._ctx_manager:
            self._ctx_manager.add_deployment(incident_id, deployment)

    def _build_rollback_deployment(
        self,
        result:      RollbackResult,
        service:     str,
        version:     str,
        environment: str,
    ) -> Deployment:
        """Build a core.models.Deployment from a RollbackResult."""
        return Deployment(
            service       = service,
            branch        = version,
            version       = version,
            deployment_id = result.deployment_id,
            status        = DeploymentStatus.ROLLED_BACK,
            started_at    = datetime.utcnow(),
            finished_at   = datetime.utcnow(), 
            metadata      = {
                "provider":    self.provider.name,
                "environment": environment,
                "rollback":    True,
            },
        )

    async def _publish_complete(
        self,
        deployment:  Deployment,
        incident_id: Optional[str],
    ) -> None:
        await self.publish(Event(
            type        = EventType.DEPLOYMENT_COMPLETE,
            source      = self.name,
            incident_id = incident_id,
            data        = {
                "deployment_id": deployment.deployment_id,
                "service":       deployment.service,
                "branch":        deployment.branch,
                "version":       deployment.version,
                "status":        deployment.status.value,
                "environment":   deployment.metadata.get("environment", ""),
                "pipeline_url":  deployment.pipeline_url or "",
            },
        ))

    async def _publish_rollback_complete(
        self,
        result:      RollbackResult,
        incident_id: Optional[str],
        version:     str,
        environment: str,
    ) -> None:
        await self.publish(Event(
            type        = EventType.DEPLOYMENT_COMPLETE,
            source      = self.name,
            incident_id = incident_id,
            data        = {
                "deployment_id":  result.deployment_id,
                "service":        result.service,
                "status":         DeploymentStatus.ROLLED_BACK.value,
                "rolled_back_to": version,
                "environment":    environment,
                "message":        result.message,
            },
        ))

    async def _publish_failed(
        self,
        service:     str,
        incident_id: Optional[str],
        error:       str,
    ) -> None:
        await self.publish(Event(
            type        = EventType.DEPLOYMENT_COMPLETE,
            source      = self.name,
            incident_id = incident_id,
            data        = {
                "service": service,
                "status":  DeploymentStatus.FAILED.value,
                "error":   error,
            },
        ))