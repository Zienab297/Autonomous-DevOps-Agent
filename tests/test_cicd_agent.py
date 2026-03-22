"""
tests/test_cicd_agent.py
=========================
Live tests for the CI/CD Agent against https://github.com/Zienab297/test-devops

Prerequisites:
    1. GITHUB_TOKEN env var set (repo + actions + deployments scope)
    2. .github/workflows/deploy.yml committed to Zienab297/test-devops
       (use the deploy.yml file from this project)

Run:
    export GITHUB_TOKEN=ghp_your_token_here
    pytest tests/test_cicd_agent.py -v
"""
from __future__ import annotations

import asyncio
import os
import time
import pytest
from dotenv import load_dotenv 

from core.orchestrator  import Orchestrator
from core.event_bus     import Event, EventType
from core.base_agent    import AgentState
from core.models        import (
    AgentStatus, Deployment, DeploymentStatus,
    Incident, Severity,
)
from agents.cicd_agent.cicd_agent         import CICDAgent
from providers.cicd.base_provider   import PipelineRun
from providers.cicd.github_provider import GitHubProvider


load_dotenv()                    # ← add this line — loads .env from project root

from core.orchestrator  import Orchestrator
# ... rest of imports

REPO         = "Zienab297/test-devops"
BRANCH       = "main"
WORKFLOW     = "deploy.yml"
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")   # ← use getenv not environ[]
if not GITHUB_TOKEN:
    pytest.exit(
        "GITHUB_TOKEN not set. Add it to your .env file:\n"
        "  GITHUB_TOKEN=ghp_your_token_here",
        returncode=1,
    )


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def orch():
    return Orchestrator()

@pytest.fixture
def provider():
    return GitHubProvider(token=GITHUB_TOKEN, org="Zienab297")

@pytest.fixture
def agent(orch, provider):
    return CICDAgent(
        provider    = provider,
        event_bus   = orch.event_bus,
        registry    = orch.registry,
        state       = orch.state_manager,
        ctx_manager = orch.context_manager,
    )

@pytest.fixture
def incident():
    return Incident(
        service     = REPO,
        severity    = Severity.HIGH,
        description = "Error rate spike on test-devops",
    )


# ── helper ────────────────────────────────────────────────────────────────────

async def wait_for_run(
    agent: CICDAgent,
    run_id: str,
    timeout: int = 120,
    interval: int = 5,
) -> PipelineRun:
    """Poll until the run finishes or timeout expires."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = await agent.get_pipeline_status(run_id, REPO)
        if run.status in ("success", "failed", "cancelled"):
            return run
        await asyncio.sleep(interval)
    raise TimeoutError(f"Run {run_id} did not finish within {timeout}s")


# ── 1. lifecycle ──────────────────────────────────────────────────────────────

class TestLifecycle:

    @pytest.mark.asyncio
    async def test_start_registers_in_registry(self, orch, agent):
        await agent.start()
        assert orch.registry.is_registered("cicd_agent")
        assert agent.state == AgentState.RUNNING
        await agent.stop()

    @pytest.mark.asyncio
    async def test_start_sets_status_running(self, orch, agent):
        await agent.start()
        assert orch.state_manager.get_agent_status("cicd_agent") == AgentStatus.RUNNING
        await agent.stop()

    @pytest.mark.asyncio
    async def test_start_subscribes_to_events(self, orch, agent):
        await agent.start()
        assert orch.event_bus.get_subscribers_count(EventType.DEPLOYMENT_STARTED) >= 1
        assert orch.event_bus.get_subscribers_count(EventType.ROLLBACK_TRIGGERED)  >= 1
        await agent.stop()

    @pytest.mark.asyncio
    async def test_stop_unregisters(self, orch, agent):
        await agent.start()
        await agent.stop()
        assert not orch.registry.is_registered("cicd_agent")
        assert agent.state == AgentState.STOPPED

    @pytest.mark.asyncio
    async def test_stop_sets_status_stopped(self, orch, agent):
        await agent.start()
        await agent.stop()
        assert orch.state_manager.get_agent_status("cicd_agent") == AgentStatus.STOPPED

    @pytest.mark.asyncio
    async def test_double_start_raises(self, agent):
        await agent.start()
        with pytest.raises(RuntimeError, match="already running"):
            await agent.start()
        await agent.stop()


# ── 2. health check ───────────────────────────────────────────────────────────

class TestHealthCheck:

    @pytest.mark.asyncio
    async def test_reaches_github_api(self, agent):
        result = await agent.health_check()
        assert result["healthy"]  is True
        assert result["provider"] == "github"
        assert result["agent"]    == "cicd_agent"


# ── 3. trigger pipeline ───────────────────────────────────────────────────────

class TestTriggerPipeline:

    @pytest.mark.asyncio
    async def test_returns_run_id(self, orch, agent):
        """workflow_dispatch fires on the real repo and returns a run_id."""
        await agent.start()

        run = await agent.trigger_pipeline(
            repo     = REPO,
            branch   = BRANCH,
            workflow = WORKFLOW,
            inputs   = {
                "environment":  "staging",
                "version":      BRANCH,
                "triggered_by": "devops-agent-sdk-test",
            },
        )

        assert run.id not in ("", "unknown")
        assert run.repo   == REPO
        assert run.branch == BRANCH
        assert run.status in ("pending", "running", "success")
        assert run.url.startswith("https://github.com/")

        await agent.stop()

    @pytest.mark.asyncio
    async def test_publishes_deployment_started_event(self, orch, agent):
        """Triggering a pipeline fires DEPLOYMENT_STARTED on the EventBus."""
        received: list[Event] = []
        async def capture(e: Event): received.append(e)
        orch.event_bus.subscribe(EventType.DEPLOYMENT_STARTED, capture)

        await agent.start()
        run = await agent.trigger_pipeline(
            repo=REPO, branch=BRANCH, workflow=WORKFLOW
        )

        started = [e for e in received if e.type == EventType.DEPLOYMENT_STARTED]
        assert len(started) >= 1
        assert started[-1].data["run_id"] == run.id

        await agent.stop()

    @pytest.mark.asyncio
    async def test_pipeline_completes_with_success(self, orch, agent):
        """
        Full round-trip: trigger → poll → success.
        The echo workflow takes ~15–20s on GitHub-hosted runners.
        Timeout: 120s.
        """
        await agent.start()

        run = await agent.trigger_pipeline(
            repo     = REPO,
            branch   = BRANCH,
            workflow = WORKFLOW,
            inputs   = {
                "environment":  "staging",
                "triggered_by": "devops-agent-sdk-test",
            },
        )

        final = await wait_for_run(agent, run.id)

        assert final.status == "success", (
            f"Pipeline {run.id} ended with '{final.status}'. "
            f"Check: {run.url}"
        )

        await agent.stop()

    @pytest.mark.asyncio
    async def test_collect_logs_after_run(self, orch, agent):
        """
        After a completed run, collect_deployment_logs returns
        the job/step summary lines from GitHub Actions.
        """
        await agent.start()

        run = await agent.trigger_pipeline(
            repo=REPO, branch=BRANCH, workflow=WORKFLOW
        )
        await wait_for_run(agent, run.id)

        logs = await agent.collect_deployment_logs(run.id, REPO)
        assert len(logs) > 0
        assert all(isinstance(line, str) for line in logs)

        # The workflow has named steps — at least one should appear
        all_text = "\n".join(logs)
        assert "Hello" in all_text or "step=" in all_text

        await agent.stop()

    @pytest.mark.asyncio
    async def test_get_pipeline_status_after_trigger(self, orch, agent):
        """get_pipeline_status returns the run we just triggered."""
        await agent.start()

        run    = await agent.trigger_pipeline(
            repo=REPO, branch=BRANCH, workflow=WORKFLOW
        )
        status = await agent.get_pipeline_status(run.id, REPO)

        assert status.id   == run.id
        assert status.repo == REPO
        assert status.status in ("pending", "running", "success", "failed")

        await agent.stop()


# ── 4. deploy ─────────────────────────────────────────────────────────────────

class TestDeploy:

    @pytest.mark.asyncio
    async def test_creates_github_deployment_record(self, orch, agent):
        """
        deploy() creates a real GitHub Deployment object via the
        Deployments API and returns a core.models.Deployment.
        """
        await agent.start()

        dep = await agent.deploy(
            service     = REPO,
            branch      = BRANCH,
            environment = "staging",
        )

        assert dep.deployment_id.startswith("DEP-GH-")
        assert dep.service == REPO
        assert dep.status  == DeploymentStatus.RUNNING
        assert dep.pipeline_url

        await agent.stop()

    @pytest.mark.asyncio
    async def test_deploy_stored_in_state_manager(self, orch, agent):
        """Deployment is immediately queryable from StateManager."""
        await agent.start()

        dep    = await agent.deploy(REPO, branch=BRANCH, environment="staging")
        stored = orch.state_manager.get_deployment(dep.deployment_id)

        assert stored is not None
        assert stored.deployment_id == dep.deployment_id
        assert stored.service       == REPO

        await agent.stop()

    @pytest.mark.asyncio
    async def test_deploy_publishes_started_and_complete_events(self, orch, agent):
        """deploy() fires both DEPLOYMENT_STARTED and DEPLOYMENT_COMPLETE."""
        received: list[Event] = []
        async def capture(e: Event): received.append(e)
        orch.event_bus.subscribe(EventType.DEPLOYMENT_STARTED,  capture)
        orch.event_bus.subscribe(EventType.DEPLOYMENT_COMPLETE, capture)

        await agent.start()
        dep = await agent.deploy(REPO, branch=BRANCH, environment="staging")

        types = [e.type for e in received]
        assert EventType.DEPLOYMENT_STARTED  in types
        assert EventType.DEPLOYMENT_COMPLETE in types

        complete = next(
            e for e in received if e.type == EventType.DEPLOYMENT_COMPLETE
        )
        assert complete.data["service"] == REPO
        assert complete.data["status"]  == DeploymentStatus.RUNNING.value

        await agent.stop()

    @pytest.mark.asyncio
    async def test_deploy_attached_to_incident_context(self, orch, agent, incident):
        """
        When incident_id is provided, the Deployment is attached to
        IncidentContext so the KnowledgeAgent can see it.
        """
        await agent.start()

        orch.state_manager.add_incident(incident)
        orch.context_manager.create_context(incident)

        dep = await agent.deploy(
            service     = REPO,
            branch      = BRANCH,
            environment = "staging",
            incident_id = incident.incident_id,
        )

        ctx = orch.context_manager.get_context(incident.incident_id)
        assert any(d.deployment_id == dep.deployment_id for d in ctx.recent_deployments)

        # Verify to_text() includes this deployment for LLM prompts
        assert dep.service in ctx.to_text()

        await agent.stop()


# ── 5. event-driven deploy ────────────────────────────────────────────────────

class TestEventDrivenDeploy:

    @pytest.mark.asyncio
    async def test_deployment_started_on_bus_calls_provider(self, orch, agent):
        """
        Publishing DEPLOYMENT_STARTED on the EventBus causes the agent
        to call the GitHub Deployments API automatically.
        """
        received: list[Event] = []
        async def capture(e: Event): received.append(e)
        orch.event_bus.subscribe(EventType.DEPLOYMENT_COMPLETE, capture)

        await agent.start()

        await orch.event_bus.publish(Event(
            type   = EventType.DEPLOYMENT_STARTED,
            source = "self_healing_agent",
            data   = {
                "service":     REPO,
                "branch":      BRANCH,
                "environment": "staging",
                "version":     "",
            },
        ))

        # Agent should have responded and published DEPLOYMENT_COMPLETE
        assert len(received) >= 1
        assert received[0].data["service"] == REPO

        # Deployment stored in StateManager
        deps = orch.state_manager.get_deployments_for_service(REPO)
        assert len(deps) >= 1

        await agent.stop()

    @pytest.mark.asyncio
    async def test_no_recursion_on_deployment_started(self, orch, agent):
        """
        DEPLOYMENT_STARTED fires once. The agent listens to it but
        deploy() also publishes it — the recursion guard must prevent loops.
        The GitHub API should be called exactly once.
        """
        deploy_complete_events: list[Event] = []
        async def capture(e: Event): deploy_complete_events.append(e)
        orch.event_bus.subscribe(EventType.DEPLOYMENT_COMPLETE, capture)

        await agent.start()

        await orch.event_bus.publish(Event(
            type   = EventType.DEPLOYMENT_STARTED,
            source = "test",
            data   = {
                "service":     REPO,
                "branch":      BRANCH,
                "environment": "staging",
                "version":     "",
            },
        ))

        # Exactly one DEPLOYMENT_COMPLETE, not a cascade
        assert len(deploy_complete_events) == 1
        await agent.stop()

    @pytest.mark.asyncio
    async def test_agent_returns_to_idle_after_event(self, orch, agent):
        """Agent status is IDLE again after handling an event."""
        await agent.start()

        await orch.event_bus.publish(Event(
            type   = EventType.DEPLOYMENT_STARTED,
            source = "test",
            data   = {
                "service":     REPO,
                "branch":      BRANCH,
                "environment": "staging",
                "version":     "",
            },
        ))

        assert orch.state_manager.get_agent_status("cicd_agent") == AgentStatus.IDLE
        await agent.stop()


# ── 6. full incident flow ─────────────────────────────────────────────────────

class TestFullIncidentFlow:

    @pytest.mark.asyncio
    async def test_incident_to_deployment_complete(self, orch, agent, incident):
        """
        End-to-end live flow:
          1. Incident + context created (as Orchestrator.handle_incident() does)
          2. Self-Healing publishes DEPLOYMENT_STARTED on the bus
          3. CICDAgent calls GitHub Deployments API
          4. Deployment stored in StateManager
          5. Deployment attached to IncidentContext
          6. DEPLOYMENT_COMPLETE fired on bus with correct service
        """
        complete_events: list[Event] = []
        async def capture(e: Event): complete_events.append(e)
        orch.event_bus.subscribe(EventType.DEPLOYMENT_COMPLETE, capture)

        await agent.start()

        # Seed incident exactly as the Orchestrator does
        orch.state_manager.add_incident(incident)
        orch.context_manager.create_context(incident)

        # Self-Healing Agent triggers a redeploy
        await orch.event_bus.publish(Event(
            type        = EventType.DEPLOYMENT_STARTED,
            source      = "self_healing_agent",
            incident_id = incident.incident_id,
            data        = {
                "service":     REPO,
                "branch":      BRANCH,
                "environment": "staging",
                "version":     "",
            },
        ))

        # 1 — deployment in StateManager
        deps = orch.state_manager.get_deployments_for_service(REPO)
        assert len(deps) >= 1

        # 2 — deployment attached to incident context
        ctx = orch.context_manager.get_context(incident.incident_id)
        assert len(ctx.recent_deployments) >= 1

        # 3 — DEPLOYMENT_COMPLETE on bus
        assert len(complete_events) >= 1
        assert complete_events[0].data["service"] == REPO

        # 4 — context to_text() includes the deployment (for KnowledgeAgent)
        prompt_text = ctx.to_text()
        assert REPO in prompt_text

        await agent.stop()