"""
tests/test_k8s_provider.py
============================
Tests for KubernetesProvider and CompositeProvider.

Uses a StubK8sProvider for all tests — no real cluster needed.
Run with:
    pytest tests/test_k8s_provider.py -v -s
"""
from __future__ import annotations

import asyncio
import pytest
from datetime import datetime
from unittest.mock import AsyncMock, patch

from core.models import Deployment, DeploymentStatus
from providers.cicd.base_provider import PipelineRun, RollbackResult


# ── StubK8sProvider ───────────────────────────────────────────────────────────

class StubK8s:
    """Simulates KubernetesProvider without kubectl."""
    name = "kubernetes"
    calls: list = []

    def _rec(self, method, **kw):
        self.calls.append({"method": method, **kw})

    async def trigger_pipeline(self, repo, branch="main", workflow="", inputs=None):
        self._rec("trigger_pipeline", repo=repo)
        return PipelineRun(
            id="k8s-restart-auth-api-120000", repo=repo, branch=branch,
            workflow=workflow or repo.split("/")[-1],
            status="running", url="kubectl rollout status", started_at=datetime.utcnow()
        )

    async def get_pipeline_status(self, run_id, repo):
        self._rec("get_pipeline_status", run_id=run_id)
        return PipelineRun(
            id=run_id, repo=repo, branch="main", workflow="auth-api",
            status="success", url="kubectl rollout status", finished_at=datetime.utcnow()
        )

    async def deploy(self, service, branch="main", environment="production", version=""):
        self._rec("deploy", service=service, version=version, environment=environment)
        return Deployment(
            service=service, branch=branch, version=version or branch,
            deployment_id=f"DEP-K8S-AUTH-120000",
            status=DeploymentStatus.RUNNING,
            pipeline_url="kubectl rollout status deployment/auth-api",
            started_at=datetime.utcnow(),
            metadata={"provider": "kubernetes", "namespace": "production"},
        )

    async def get_deployment_status(self, deployment_id):
        self._rec("get_deployment_status", deployment_id=deployment_id)
        return Deployment(
            service="auth-api", branch="main", version="v2.3.1",
            deployment_id=deployment_id, status=DeploymentStatus.SUCCESS,
            finished_at=datetime.utcnow(),
        )

    async def rollback(self, service, version, environment="production"):
        self._rec("rollback", service=service, version=version)
        return RollbackResult(
            deployment_id=f"DEP-K8S-AUTH-RB-120000", service=service,
            rolled_back_to=f"revision {version}" if version.isdigit() else "previous revision",
            status=DeploymentStatus.ROLLED_BACK,
            message=f"Rolled back {service}",
        )

    async def collect_logs(self, run_id, repo):
        self._rec("collect_logs", run_id=run_id)
        return [
            "[auth-api-pod] INFO  Server started on :8080",
            "[auth-api-pod] ERROR JWT verification failed",
            "[auth-api-pod] INFO  Rolling update applied",
        ]

    async def health_check(self):
        return True


class StubGitHub:
    """Simulates GitHubProvider."""
    name = "github"
    calls: list = []

    def _rec(self, method, **kw):
        self.calls.append({"method": method, **kw})

    async def deploy(self, service, branch="main", environment="production", version=""):
        self._rec("deploy", service=service)
        return Deployment(
            service=service, branch=branch, version=version or branch,
            deployment_id="DEP-GH-ABCD1234",
            status=DeploymentStatus.RUNNING,
            pipeline_url="https://github.com/org/repo/deployments",
            started_at=datetime.utcnow(),
        )

    async def rollback(self, service, version, environment="production"):
        self._rec("rollback", service=service)
        return RollbackResult(
            deployment_id="DEP-GH-ROLLBACK",
            service=service, rolled_back_to=version,
            status=DeploymentStatus.ROLLED_BACK,
            message=f"GitHub rollback record for {service}",
        )

    async def collect_logs(self, run_id, repo):
        self._rec("collect_logs")
        return ["[GitHub] workflow step=deploy conclusion=success"]

    async def health_check(self):
        return True

    async def trigger_pipeline(self, repo, branch="main", workflow="", inputs=None):
        self._rec("trigger_pipeline")
        return PipelineRun(
            id="run-99999", repo=repo, branch=branch,
            workflow=workflow, status="running",
            url="https://github.com/org/repo/actions/runs/99999",
            started_at=datetime.utcnow(),
        )

    async def get_pipeline_status(self, run_id, repo):
        self._rec("get_pipeline_status")
        return PipelineRun(
            id=run_id, repo=repo, branch="main", workflow="deploy.yml",
            status="success", url="https://github.com", finished_at=datetime.utcnow()
        )


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def stub_k8s():
    s = StubK8s()
    s.calls = []
    return s

@pytest.fixture
def stub_github():
    s = StubGitHub()
    s.calls = []
    return s

@pytest.fixture
def composite(stub_github, stub_k8s):
    from providers.cicd.multi_provider import CompositeProvider
    return CompositeProvider(github=stub_github, k8s=stub_k8s)

@pytest.fixture
def orch():
    from core.orchestrator import Orchestrator
    return Orchestrator()

@pytest.fixture
def k8s_agent(orch, stub_k8s):
    from agents.cicd_agent.cicd_agent import CICDAgent
    return CICDAgent(
        provider    = stub_k8s,
        event_bus   = orch.event_bus,
        registry    = orch.registry,
        state       = orch.state_manager,
        ctx_manager = orch.context_manager,
    )

@pytest.fixture
def composite_agent(orch, composite):
    from agents.cicd_agent.cicd_agent import CICDAgent
    return CICDAgent(
        provider    = composite,
        event_bus   = orch.event_bus,
        registry    = orch.registry,
        state       = orch.state_manager,
        ctx_manager = orch.context_manager,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# KubernetesProvider unit tests (stub)
# ═══════════════════════════════════════════════════════════════════════════════

class TestKubernetesProviderStub:

    @pytest.mark.asyncio
    async def test_deploy_returns_k8s_deployment(self, stub_k8s):
        dep = await stub_k8s.deploy(
            service="auth-api", version="v2.3.1", environment="production"
        )
        assert dep.deployment_id.startswith("DEP-K8S-")
        assert dep.status == DeploymentStatus.RUNNING
        assert dep.metadata["provider"] == "kubernetes"
        assert dep.metadata["namespace"] == "production"

    @pytest.mark.asyncio
    async def test_rollback_revision_number(self, stub_k8s):
        result = await stub_k8s.rollback(
            service="auth-api", version="3", environment="production"
        )
        assert result.status == DeploymentStatus.ROLLED_BACK
        assert "revision 3" in result.rolled_back_to
        assert result.service == "auth-api"

    @pytest.mark.asyncio
    async def test_rollback_previous(self, stub_k8s):
        result = await stub_k8s.rollback(
            service="auth-api", version="main", environment="production"
        )
        assert result.status == DeploymentStatus.ROLLED_BACK
        assert "previous" in result.rolled_back_to

    @pytest.mark.asyncio
    async def test_collect_logs_returns_pod_lines(self, stub_k8s):
        logs = await stub_k8s.collect_logs("k8s-restart-auth-api", "auth-api")
        assert len(logs) > 0
        assert all(isinstance(l, str) for l in logs)

    @pytest.mark.asyncio
    async def test_trigger_pipeline_restart(self, stub_k8s):
        run = await stub_k8s.trigger_pipeline(
            repo="org/auth-api", branch="main", workflow="auth-api"
        )
        assert run.id.startswith("k8s-restart-")
        assert run.status == "running"

    @pytest.mark.asyncio
    async def test_get_pipeline_status_success(self, stub_k8s):
        status = await stub_k8s.get_pipeline_status(
            "k8s-restart-auth-api-120000", "auth-api"
        )
        assert status.status == "success"

    @pytest.mark.asyncio
    async def test_health_check(self, stub_k8s):
        assert await stub_k8s.health_check() is True


# ═══════════════════════════════════════════════════════════════════════════════
# CompositeProvider tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestCompositeProvider:

    @pytest.mark.asyncio
    async def test_deploy_calls_both_providers(self, composite, stub_github, stub_k8s):
        dep = await composite.deploy(
            service="auth-api", version="v2.3.1", environment="production"
        )
        # Both providers were called
        assert any(c["method"] == "deploy" for c in stub_k8s.calls)
        assert any(c["method"] == "deploy" for c in stub_github.calls)
        # K8s result is primary
        assert dep.deployment_id.startswith("DEP-K8S-")
        assert dep.status == DeploymentStatus.RUNNING

    @pytest.mark.asyncio
    async def test_deploy_merges_metadata(self, composite):
        dep = await composite.deploy(service="auth-api", version="v2.3.1")
        assert dep.metadata.get("providers") == "github+kubernetes"
        assert "github_deployment_id" in dep.metadata
        assert "k8s_deployment_id"    in dep.metadata

    @pytest.mark.asyncio
    async def test_rollback_calls_both(self, composite, stub_github, stub_k8s):
        result = await composite.rollback(
            service="auth-api", version="3", environment="production"
        )
        assert any(c["method"] == "rollback" for c in stub_k8s.calls)
        assert any(c["method"] == "rollback" for c in stub_github.calls)
        assert result.status == DeploymentStatus.ROLLED_BACK

    @pytest.mark.asyncio
    async def test_rollback_k8s_preferred(self, composite):
        result = await composite.rollback(service="auth-api", version="3")
        # K8s result has revision in rolled_back_to
        assert "revision" in result.rolled_back_to

    @pytest.mark.asyncio
    async def test_collect_logs_merges_both(self, composite):
        logs = await composite.collect_logs("run-99999", "auth-api")
        combined = "\n".join(logs)
        assert "Kubernetes pod logs" in combined
        assert len(logs) > 2

    @pytest.mark.asyncio
    async def test_trigger_pipeline_uses_github(self, composite, stub_github):
        run = await composite.trigger_pipeline(
            repo="org/auth-api", branch="main", workflow="deploy.yml"
        )
        assert any(c["method"] == "trigger_pipeline" for c in stub_github.calls)
        assert run.status == "running"

    @pytest.mark.asyncio
    async def test_health_check_true_if_one_ok(self, stub_github, stub_k8s):
        from providers.cicd.multi_provider import CompositeProvider

        stub_k8s_broken = StubK8s()
        stub_k8s_broken.health_check = AsyncMock(return_value=False)

        provider = CompositeProvider(github=stub_github, k8s=stub_k8s_broken)
        assert await provider.health_check() is True   # GitHub still up

    @pytest.mark.asyncio
    async def test_composite_requires_at_least_one_provider(self):
        from providers.cicd.multi_provider import CompositeProvider
        with pytest.raises(ValueError):
            CompositeProvider(github=None, k8s=None)

    @pytest.mark.asyncio
    async def test_name_reflects_active_providers(self, composite):
        assert composite.name == "github+kubernetes"

    @pytest.mark.asyncio
    async def test_k8s_only_provider(self, stub_k8s):
        from providers.cicd.multi_provider import CompositeProvider
        provider = CompositeProvider(k8s=stub_k8s)
        assert provider.name == "kubernetes"

    @pytest.mark.asyncio
    async def test_deploy_succeeds_if_github_fails(self, stub_k8s):
        """K8s deploy succeeds even if GitHub record fails."""
        from providers.cicd.multi_provider import CompositeProvider

        broken_github = StubGitHub()
        broken_github.deploy = AsyncMock(side_effect=RuntimeError("GitHub down"))

        provider = CompositeProvider(github=broken_github, k8s=stub_k8s)
        dep = await provider.deploy(service="auth-api", version="v2.3.1")

        assert dep.deployment_id.startswith("DEP-K8S-")
        assert dep.status == DeploymentStatus.RUNNING


# ═══════════════════════════════════════════════════════════════════════════════
# CICDAgent + K8s wired together
# ═══════════════════════════════════════════════════════════════════════════════

class TestCICDAgentWithK8s:

    @pytest.mark.asyncio
    async def test_agent_starts_with_k8s_provider(self, orch, k8s_agent):
        from core.base_agent import AgentState
        await k8s_agent.start()
        assert k8s_agent.state == AgentState.RUNNING
        result = await k8s_agent.health_check()
        assert result["provider"] == "kubernetes"
        assert result["healthy"] is True
        await k8s_agent.stop()

    @pytest.mark.asyncio
    async def test_deploy_via_agent_stored_in_state(self, orch, k8s_agent):
        await k8s_agent.start()
        dep = await k8s_agent.deploy(
            service="auth-api", branch="main",
            environment="production", version="v2.3.1",
        )
        assert dep.deployment_id.startswith("DEP-K8S-")
        stored = orch.state_manager.get_deployment(dep.deployment_id)
        assert stored is not None
        assert stored.status == DeploymentStatus.RUNNING
        await k8s_agent.stop()

    @pytest.mark.asyncio
    async def test_rollback_via_agent_stored_rolled_back(self, orch, k8s_agent):
        await k8s_agent.start()
        result = await k8s_agent.rollback(
            service="auth-api", version="3", environment="production"
        )
        assert result.status == DeploymentStatus.ROLLED_BACK
        stored = orch.state_manager.get_deployment(result.deployment_id)
        assert stored is not None
        assert stored.status == DeploymentStatus.ROLLED_BACK
        await k8s_agent.stop()

    @pytest.mark.asyncio
    async def test_composite_agent_deploy(self, orch, composite_agent):
        await composite_agent.start()
        dep = await composite_agent.deploy(
            service="auth-api", branch="main",
            environment="production", version="v2.3.1",
        )
        assert dep.deployment_id.startswith("DEP-K8S-")
        assert dep.metadata.get("providers") == "github+kubernetes"
        await composite_agent.stop()

    @pytest.mark.asyncio
    async def test_composite_agent_logs_from_both(self, orch, composite_agent):
        await composite_agent.start()
        run = await composite_agent.trigger_pipeline(
            repo="org/auth-api", branch="main"
        )
        logs = await composite_agent.collect_deployment_logs(run.id, "org/auth-api")
        assert len(logs) > 0
        await composite_agent.stop()

    @pytest.mark.asyncio
    async def test_event_driven_k8s_deploy(self, orch, k8s_agent):
        from core.event_bus import Event, EventType
        received = []
        async def capture(e): received.append(e)
        orch.event_bus.subscribe(EventType.DEPLOYMENT_COMPLETE, capture)

        await k8s_agent.start()
        await orch.event_bus.publish(Event(
            type   = EventType.DEPLOYMENT_STARTED,
            source = "self_healing_agent",
            data   = {
                "service":     "auth-api",
                "branch":      "main",
                "environment": "production",
                "version":     "v2.3.1",
            },
        ))

        assert len(received) >= 1
        assert received[0].data["service"] == "auth-api"
        await k8s_agent.stop()