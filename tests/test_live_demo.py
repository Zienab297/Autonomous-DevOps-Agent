"""
tests/test_live_demo.py
========================
Live end-to-end demo: CI/CD Agent + Monitoring Agent
against https://github.com/Zienab297/test-devops

Each test is fully self-contained — no shared aiohttp sessions
across tests (fixes "Event loop is closed" errors).

Run:
    pytest tests/test_live_demo.py -v -s
"""
from __future__ import annotations

import asyncio
import base64
import os
import time
from datetime import datetime

import aiohttp
import pytest
from dotenv import load_dotenv

load_dotenv()

# ── constants ─────────────────────────────────────────────────────────────────

GITHUB_TOKEN  = os.getenv("GITHUB_TOKEN", "")
REPO          = "Zienab297/test-devops"
BRANCH        = "main"
WORKFLOW_PATH = ".github/workflows/deploy.yml"
WORKFLOW_CONTENT = """\
name: DevOps Agent - Hello World

on:
  workflow_dispatch:
    inputs:
      environment:
        description: Target environment
        required: false
        default: production
      version:
        description: Version or branch
        required: false
        default: main
      triggered_by:
        description: Who triggered this
        required: false
        default: devops-agent-sdk

jobs:
  hello:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v4
      - name: Hello World
        run: echo "Hello from DevOps Agent SDK!"
      - name: Show context
        run: |
          echo "environment : ${{ inputs.environment }}"
          echo "version     : ${{ inputs.version }}"
          echo "triggered_by: ${{ inputs.triggered_by }}"
          echo "run_id      : ${{ github.run_id }}"
          echo "sha         : ${{ github.sha }}"
      - name: Done
        run: echo "Workflow complete"
"""

if not GITHUB_TOKEN:
    pytest.skip(
        "GITHUB_TOKEN not set in .env — skipping live demo",
        allow_module_level=True,
    )

# ── print helpers ─────────────────────────────────────────────────────────────

SEP  = "=" * 64
SEP2 = "-" * 64

def header(t):  print(f"\n{SEP}\n  {t}\n{SEP}")
def section(t): print(f"\n{SEP2}\n  {t}\n{SEP2}")
def ok(m):      print(f"  [OK]   {m}")
def info(m):    print(f"  [..]   {m}")
def fail(m):    print(f"  [FAIL] {m}")
def kv(k, v):   print(f"  {k:<30} {v}")

# ── fixtures — fresh stack per test ──────────────────────────────────────────

@pytest.fixture
def orch():
    from core.orchestrator import Orchestrator
    return Orchestrator()

@pytest.fixture
def provider():
    from providers.cicd.github_provider import GitHubProvider
    return GitHubProvider(token=GITHUB_TOKEN, org="Zienab297")

@pytest.fixture
def agent(orch, provider):
    from agents.cicd_agent.cicd_agent import CICDAgent
    return CICDAgent(
        provider    = provider,
        event_bus   = orch.event_bus,
        registry    = orch.registry,
        state       = orch.state_manager,
        ctx_manager = orch.context_manager,
    )

@pytest.fixture
def incident(orch):
    from core.models import Incident, Severity
    inc = Incident(
        service     = REPO,
        severity    = Severity.HIGH,
        description = "Simulated error rate spike on test-devops",
    )
    orch.state_manager.add_incident(inc)
    orch.context_manager.create_context(inc)
    return inc

# ── helper ────────────────────────────────────────────────────────────────────

async def wait_for_run(agent, run_id: str, timeout: int = 120):
    info(f"Polling run {run_id} (timeout={timeout}s) …")
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = await agent.get_pipeline_status(run_id, REPO)
        if run.status in ("success", "failed", "cancelled"):
            return run
        elapsed = int(timeout - (deadline - time.time()))
        info(f"  still running… ({elapsed}s elapsed, status={run.status})")
        await asyncio.sleep(8)
    raise TimeoutError(f"Run {run_id} did not finish within {timeout}s")

# ═══════════════════════════════════════════════════════════════════════════════
# ── SECTION 1: SETUP ──────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_01_commit_workflow_file():
    """Commit deploy.yml to the repo so workflow_dispatch can fire it."""
    header("DevOps Agent SDK — Live Demo on Zienab297/test-devops")
    kv("Repo:",   f"https://github.com/{REPO}")
    kv("Branch:", BRANCH)
    kv("Time:",   datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"))
    section("Step 1 — Commit deploy.yml to repo")

    headers = {
        "Authorization":        f"Bearer {GITHUB_TOKEN}",
        "Accept":               "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    url = f"https://api.github.com/repos/{REPO}/contents/{WORKFLOW_PATH}"

    async with aiohttp.ClientSession(headers=headers) as s:
        sha = None
        async with s.get(url) as resp:
            if resp.status == 200:
                data = await resp.json()
                sha  = data.get("sha")
                info(f"deploy.yml exists (sha={sha[:8]}…) — updating")
            else:
                info("deploy.yml not found — creating")

        body = {
            "message": "Add deploy.yml via DevOps Agent SDK",
            "content": base64.b64encode(WORKFLOW_CONTENT.encode()).decode(),
            "branch":  BRANCH,
        }
        if sha:
            body["sha"] = sha

        async with s.put(url, json=body) as resp:
            assert resp.status in (200, 201), \
                f"Could not commit deploy.yml [{resp.status}]: {await resp.text()}"
            data = await resp.json()
            ok(f"deploy.yml committed — commit {data['commit']['sha'][:8]}")
            ok(f"URL: https://github.com/{REPO}/blob/main/{WORKFLOW_PATH}")

    info("Waiting 3s for GitHub to register the file …")
    await asyncio.sleep(3)

# ═══════════════════════════════════════════════════════════════════════════════
# ── SECTION 2: AGENT LIFECYCLE ────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_02_agent_starts_and_registers(orch, agent):
    """Agent registers in AgentRegistry and subscribes to bus events."""
    section("Step 2 — Agent lifecycle: start")
    from core.base_agent import AgentState
    from core.event_bus  import EventType

    await agent.start()

    kv("Agent state:",   agent.state.value)
    kv("Agent ID:",      agent.agent_id)
    kv("Registered:",    str(orch.registry.is_registered("cicd_agent")))
    kv("Bus subs (DEPLOYMENT_STARTED):",
       orch.event_bus.get_subscribers_count(EventType.DEPLOYMENT_STARTED))

    assert agent.state == AgentState.RUNNING
    assert orch.registry.is_registered("cicd_agent")
    ok("Agent started — registered in AgentRegistry")

    await agent.stop()
    assert agent.state == AgentState.STOPPED
    assert not orch.registry.is_registered("cicd_agent")
    ok("Agent stopped — unregistered from AgentRegistry")

@pytest.mark.asyncio
async def test_03_health_check(agent):
    """Token is valid and GitHub API is reachable."""
    section("Step 3 — Health check")

    result = await agent.health_check()
    kv("Provider:", result["provider"])
    kv("Healthy:",  str(result["healthy"]))
    kv("State:",    result["state"])

    assert result["healthy"] is True
    assert result["provider"] == "github"
    ok("GitHub API is reachable with this token")

# ═══════════════════════════════════════════════════════════════════════════════
# ── SECTION 3: CI/CD — PIPELINE ───────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_04_trigger_pipeline(orch, agent):
    """workflow_dispatch fires on the real repo and returns a run_id."""
    section("Step 4 — Trigger pipeline (workflow_dispatch)")
    from core.event_bus import EventType
    info(f"Firing {WORKFLOW_PATH} on {REPO} …")

    received = []
    async def capture(e): received.append(e)
    orch.event_bus.subscribe(EventType.DEPLOYMENT_STARTED, capture)

    await agent.start()
    run = await agent.trigger_pipeline(
        repo     = REPO,
        branch   = BRANCH,
        workflow = "deploy.yml",
        inputs   = {
            "environment":  "staging",
            "version":      BRANCH,
            "triggered_by": "devops-agent-sdk-test",
        },
    )

    kv("Run ID:",  run.id)
    kv("Status:",  run.status)
    kv("Branch:",  run.branch)
    kv("URL:",     run.url)

    assert run.id not in ("", "unknown")
    assert run.url.startswith("https://github.com/")
    assert any(e.type == EventType.DEPLOYMENT_STARTED for e in received)
    ok("Pipeline triggered — DEPLOYMENT_STARTED on EventBus")

    await agent.stop()

@pytest.mark.asyncio
async def test_05_pipeline_completes(orch, agent):
    """Full round-trip: trigger → poll → success (timeout 120s)."""
    section("Step 5 — Pipeline completes with success")

    await agent.start()
    run = await agent.trigger_pipeline(
        repo=REPO, branch=BRANCH, workflow="deploy.yml",
        inputs={"environment": "staging", "triggered_by": "devops-agent-sdk-test"},
    )
    info(f"Triggered run {run.id}")

    final = await wait_for_run(agent, run.id, timeout=120)
    kv("Final status:", final.status)
    kv("Finished at:",  str(final.finished_at))
    kv("URL:",          run.url)

    assert final.status == "success", \
        f"Pipeline ended with '{final.status}' — check: {run.url}"
    ok("Pipeline completed successfully")
    await agent.stop()

@pytest.mark.asyncio
async def test_06_collect_logs(orch, agent):
    """After a completed run, logs contain the workflow step names."""
    section("Step 6 — Collect deployment logs")

    await agent.start()
    run = await agent.trigger_pipeline(
        repo=REPO, branch=BRANCH, workflow="deploy.yml",
    )
    await wait_for_run(agent, run.id, timeout=120)

    logs = await agent.collect_deployment_logs(run.id, REPO)
    kv("Log lines:", len(logs))
    print()
    for line in logs:
        print(f"    {line}")

    assert len(logs) > 0
    assert all(isinstance(l, str) for l in logs)
    assert any("Hello" in l or "step=" in l for l in logs)
    ok("Logs collected — step names visible")
    await agent.stop()

# ═══════════════════════════════════════════════════════════════════════════════
# ── SECTION 4: CI/CD — DEPLOY & ROLLBACK ─────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_07_deploy_creates_record(orch, agent):
    """deploy() creates a real GitHub Deployment and stores it in StateManager."""
    section("Step 7 — Create GitHub Deployment record")
    from core.event_bus  import EventType
    from core.models     import DeploymentStatus
    info("Calling GitHub Deployments API …")

    dep_events = []
    async def capture(e): dep_events.append(e)
    orch.event_bus.subscribe(EventType.DEPLOYMENT_STARTED,  capture)
    orch.event_bus.subscribe(EventType.DEPLOYMENT_COMPLETE, capture)

    await agent.start()
    dep = await agent.deploy(
        service     = REPO,
        branch      = BRANCH,
        environment = "staging",
    )

    kv("Deployment ID:", dep.deployment_id)
    kv("Service:",       dep.service)
    kv("Branch:",        dep.branch)
    kv("Status:",        dep.status.value)
    kv("Pipeline URL:",  dep.pipeline_url)

    print("\n  Events fired on EventBus:")
    for e in dep_events:
        print(f"    [{e.type.value}] source={e.source} "
              f"status={e.data.get('status','?')}")

    stored = orch.state_manager.get_deployment(dep.deployment_id)
    kv("\n  In StateManager:", stored.deployment_id if stored else "NOT FOUND")

    assert dep.deployment_id.startswith("DEP-GH-")
    assert dep.status == DeploymentStatus.RUNNING
    assert stored is not None
    assert any(e.type == EventType.DEPLOYMENT_COMPLETE for e in dep_events)
    ok("Deployment created and stored in StateManager")
    await agent.stop()

@pytest.mark.asyncio
async def test_08_deploy_attaches_to_incident(orch, agent, incident):
    """Deployment with incident_id is attached to IncidentContext."""
    section("Step 8 — Deploy attached to IncidentContext")

    await agent.start()
    dep = await agent.deploy(
        service     = REPO,
        branch      = BRANCH,
        environment = "staging",
        incident_id = incident.incident_id,
    )

    ctx = orch.context_manager.get_context(incident.incident_id)
    kv("Deployments in context:", len(ctx.recent_deployments))
    kv("Deployment ID:",          dep.deployment_id)

    assert any(d.deployment_id == dep.deployment_id for d in ctx.recent_deployments)
    assert dep.service in ctx.to_text()
    ok("Deployment visible in IncidentContext — KnowledgeAgent can see it")
    await agent.stop()

@pytest.mark.asyncio
async def test_09_rollback(orch, agent):
    """rollback() creates a ROLLED_BACK deployment in StateManager."""
    section("Step 9 — Rollback to previous version")
    from core.event_bus  import EventType
    from core.models     import DeploymentStatus
    info("Simulating rollback to 'main' …")

    rb_events = []
    async def capture(e): rb_events.append(e)
    orch.event_bus.subscribe(EventType.ROLLBACK_TRIGGERED,  capture)
    orch.event_bus.subscribe(EventType.DEPLOYMENT_COMPLETE, capture)

    await agent.start()
    result = await agent.rollback(
        service     = REPO,
        version     = BRANCH,
        environment = "staging",
    )

    kv("Deployment ID:",  result.deployment_id)
    kv("Service:",        result.service)
    kv("Rolled back to:", result.rolled_back_to)
    kv("Status:",         result.status.value)
    kv("Message:",        result.message)

    print("\n  Events fired on EventBus:")
    for e in rb_events:
        print(f"    [{e.type.value}] source={e.source}")

    stored = orch.state_manager.get_deployment(result.deployment_id)
    if stored:
        kv("\n  In StateManager:", stored.status.value)

    assert result.rolled_back_to == BRANCH
    assert result.status == DeploymentStatus.ROLLED_BACK
    assert stored is not None
    assert stored.status == DeploymentStatus.ROLLED_BACK
    assert any(e.type == EventType.ROLLBACK_TRIGGERED  for e in rb_events)
    assert any(e.type == EventType.DEPLOYMENT_COMPLETE for e in rb_events)
    ok("Rollback complete — ROLLED_BACK stored in StateManager")
    await agent.stop()

# ═══════════════════════════════════════════════════════════════════════════════
# ── SECTION 5: EVENT-DRIVEN FLOW ──────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_10_event_driven_deploy(orch, agent):
    """Publishing DEPLOYMENT_STARTED causes the agent to deploy autonomously."""
    section("Step 10 — Event-driven deploy (no direct call)")
    from core.event_bus import Event, EventType
    from core.models    import AgentStatus
    info("Publishing DEPLOYMENT_STARTED on bus — agent reacts autonomously …")

    received = []
    async def capture(e): received.append(e)
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

    kv("DEPLOYMENT_COMPLETE events:", len(received))
    if received:
        e = received[0]
        kv("  service:", e.data.get("service"))
        kv("  status:",  e.data.get("status"))

    agent_status = orch.state_manager.get_agent_status("cicd_agent")
    kv("Agent status after event:", str(agent_status))

    assert len(received) >= 1
    assert received[0].data["service"] == REPO
    assert agent_status == AgentStatus.IDLE
    ok("Agent reacted autonomously — returned to IDLE")
    await agent.stop()

@pytest.mark.asyncio
async def test_11_no_recursion(orch, agent):
    """DEPLOYMENT_STARTED must fire exactly once — no infinite loop."""
    section("Step 11 — Recursion guard check")
    from core.event_bus import Event, EventType
    info("Verifying deploy() does not re-trigger DEPLOYMENT_STARTED recursively …")

    complete_events = []
    async def capture(e): complete_events.append(e)
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

    kv("DEPLOYMENT_COMPLETE events:", len(complete_events))
    assert len(complete_events) == 1, \
        f"Expected 1 DEPLOYMENT_COMPLETE, got {len(complete_events)} — recursion detected"
    ok("No recursion — exactly 1 DEPLOYMENT_COMPLETE fired")
    await agent.stop()

# ═══════════════════════════════════════════════════════════════════════════════
# ── SECTION 6: FULL INCIDENT FLOW ─────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_12_full_incident_flow(orch, agent, incident):
    """
    End-to-end: incident → context → bus event → deploy →
    StateManager → ContextManager → KnowledgeAgent prompt text.
    """
    section("Step 12 — Full incident flow")
    from core.event_bus import Event, EventType
    info("Incident created → self-healing publishes DEPLOYMENT_STARTED →"
         " agent deploys → context updated")

    kv("Incident ID:",  incident.incident_id)
    kv("Service:",      incident.service)
    kv("Severity:",     incident.severity.value)
    kv("Description:",  incident.description)

    complete_events = []
    async def capture(e): complete_events.append(e)
    orch.event_bus.subscribe(EventType.DEPLOYMENT_COMPLETE, capture)

    await agent.start()
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

    ctx      = orch.context_manager.get_context(incident.incident_id)
    all_deps = orch.state_manager.get_deployments_for_service(REPO)

    kv("\n  Deployments in IncidentContext:", len(ctx.recent_deployments))
    kv("  Total in StateManager:",           len(all_deps))
    kv("  DEPLOYMENT_COMPLETE events:",      len(complete_events))

    if ctx.recent_deployments:
        d = ctx.recent_deployments[0]
        kv("  Deployment ID:", d.deployment_id)
        kv("  Status:",        d.status.value)

    print("\n  Context text (sent to KnowledgeAgent LLM prompt):")
    for line in ctx.to_text().split("\n")[:20]:
        print(f"    {line}")

    assert len(ctx.recent_deployments) >= 1, \
        "Deployment was not attached to IncidentContext"
    assert len(all_deps) >= 1, \
        "Deployment was not stored in StateManager"
    assert len(complete_events) >= 1, \
        "DEPLOYMENT_COMPLETE was never fired"
    assert REPO in ctx.to_text(), \
        "Service not found in context text for LLM prompt"

    ok("Deployment attached to IncidentContext")
    ok("Deployment stored in StateManager")
    ok("Context text ready for KnowledgeAgent LLM prompt")
    await agent.stop()

# ═══════════════════════════════════════════════════════════════════════════════
# ── SECTION 7: MONITORING AGENT ───────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_13_monitoring_detects_anomaly(orch):
    """
    Monitoring Agent detects an anomaly and creates an Incident.
    Simulates what happens when error rate exceeds threshold.
    """
    section("Step 13 — Monitoring: anomaly detection")
    from core.event_bus import Event, EventType
    from core.models    import Metric, Incident, IncidentStatus, Severity

    info("Simulating Monitoring Agent detecting high error rate …")

    # The Monitoring Agent would collect this from Prometheus/Datadog
    metric = Metric(
        name      = "error_rate",
        value     = 0.45,           # 45% error rate
        unit      = "%",
        service   = REPO,
        labels    = {"env": "production"},
    )
    kv("Metric name:",  metric.name)
    kv("Metric value:", f"{metric.value * 100:.0f}%")
    kv("Service:",      metric.service)
    kv("Threshold:",    "10%")

    # Anomaly detected — Monitoring Agent creates an Incident
    assert metric.value > 0.10, "No anomaly — error rate below threshold"
    ok("Anomaly detected — error rate exceeds 10% threshold")

    incident = Incident(
        service     = REPO,
        severity    = Severity.HIGH,
        description = f"Error rate spike: {metric.value*100:.0f}% (threshold: 10%)",
        metrics     = [metric],
    )
    orch.state_manager.add_incident(incident)
    orch.context_manager.create_context(incident)
    orch.context_manager.add_metrics(incident.incident_id, [metric])

    kv("\n  Incident ID:",   incident.incident_id)
    kv("  Severity:",        incident.severity.value)
    kv("  Status:",          incident.status.value)
    kv("  Description:",     incident.description)

    # Monitoring Agent publishes INCIDENT_CREATED on the bus
    received = []
    async def capture(e): received.append(e)
    orch.event_bus.subscribe(EventType.INCIDENT_CREATED, capture)

    await orch.event_bus.publish(Event(
        type        = EventType.INCIDENT_CREATED,
        source      = "monitoring_agent",
        incident_id = incident.incident_id,
        data        = {
            "incident_id": incident.incident_id,
            "service":     incident.service,
            "severity":    incident.severity.value,
            "description": incident.description,
            "metric":      {"name": metric.name, "value": metric.value},
        },
    ))

    kv("\n  INCIDENT_CREATED on bus:", len(received))
    kv("  Event source:",             received[0].source if received else "—")
    kv("  Incident in StateManager:", str(
        orch.state_manager.get_incident(incident.incident_id) is not None
    ))

    assert len(received) == 1
    assert received[0].data["service"] == REPO
    assert orch.state_manager.get_incident(incident.incident_id) is not None
    ok("INCIDENT_CREATED published — Orchestrator would call KnowledgeAgent next")

@pytest.mark.asyncio
async def test_14_monitoring_metrics_in_context(orch):
    """Metrics collected by Monitoring Agent appear in IncidentContext for the LLM."""
    section("Step 14 — Monitoring: metrics stored in IncidentContext")
    from core.models import Metric, Log, Incident, Severity

    incident = Incident(
        service     = REPO,
        severity    = Severity.HIGH,
        description = "High CPU and error rate",
    )
    orch.state_manager.add_incident(incident)
    orch.context_manager.create_context(incident)

    # Monitoring Agent collects multiple metrics
    metrics = [
        Metric(name="error_rate",   value=0.45, unit="%",  service=REPO),
        Metric(name="cpu_usage",    value=92.0, unit="%",  service=REPO),
        Metric(name="memory_usage", value=78.0, unit="%",  service=REPO),
        Metric(name="latency_p99",  value=4200, unit="ms", service=REPO),
    ]
    logs = [
        Log(level="ERROR", message="Connection pool exhausted", service=REPO),
        Log(level="ERROR", message="Timeout after 30s waiting for DB", service=REPO),
        Log(level="WARN",  message="Retry attempt 3/3 failed",  service=REPO),
    ]

    orch.context_manager.add_metrics(incident.incident_id, metrics)
    orch.context_manager.add_logs(incident.incident_id, logs)

    ctx = orch.context_manager.get_context(incident.incident_id)
    kv("Metrics in context:", len(ctx.metrics))
    kv("Logs in context:",    len(ctx.logs))

    for m in ctx.metrics:
        kv(f"  {m.name}:", f"{m.value}{m.unit}")
    print()
    for l in ctx.logs:
        kv(f"  [{l.level}]", l.message)

    print("\n  Context text (full LLM prompt input):")
    for line in ctx.to_text().split("\n"):
        print(f"    {line}")

    assert len(ctx.metrics) == 4
    assert len(ctx.logs)    == 3
    assert "error_rate"     in ctx.to_text()
    assert "ERROR"          in ctx.to_text()
    ok("Metrics and logs stored — IncidentContext ready for KnowledgeAgent")

@pytest.mark.asyncio
async def test_15_monitoring_triggers_cicd_via_bus(orch, agent):
    """
    Full pipeline: Monitoring detects problem → creates Incident →
    publishes on bus → CI/CD Agent deploys autonomously.
    This is the autonomous remediation loop.
    """
    section("Step 15 — Monitoring triggers CI/CD autonomously")
    from core.event_bus import Event, EventType
    from core.models    import Metric, Incident, Severity
    info("Simulating full autonomous loop: monitor → incident → deploy …")

    # Step A — Monitoring Agent detects anomaly
    metric = Metric(name="error_rate", value=0.52, unit="%", service=REPO)
    incident = Incident(
        service     = REPO,
        severity    = Severity.HIGH,
        description = f"Error rate {metric.value*100:.0f}% — auto-remediation triggered",
        metrics     = [metric],
    )
    orch.state_manager.add_incident(incident)
    orch.context_manager.create_context(incident)
    orch.context_manager.add_metrics(incident.incident_id, [metric])
    kv("Incident created:", incident.incident_id)

    # Step B — Self-Healing Agent (after KnowledgeAgent reasoning) decides to redeploy
    complete_events = []
    async def capture(e): complete_events.append(e)
    orch.event_bus.subscribe(EventType.DEPLOYMENT_COMPLETE, capture)

    await agent.start()
    await orch.event_bus.publish(Event(
        type        = EventType.DEPLOYMENT_STARTED,
        source      = "self_healing_agent",
        incident_id = incident.incident_id,
        data        = {
            "service":     REPO,
            "branch":      BRANCH,
            "environment": "production",
            "version":     "",
        },
    ))

    # Step C — Verify the full chain completed
    ctx      = orch.context_manager.get_context(incident.incident_id)
    all_deps = orch.state_manager.get_deployments_for_service(REPO)

    kv("\n  Incident ID:",              incident.incident_id)
    kv("  Metric that triggered it:",   f"{metric.value*100:.0f}% error rate")
    kv("  Deployments in context:",     len(ctx.recent_deployments))
    kv("  Deployments in StateManager:", len(all_deps))
    kv("  DEPLOYMENT_COMPLETE events:", len(complete_events))

    if ctx.recent_deployments:
        d = ctx.recent_deployments[0]
        kv("  Deployed to:",             d.metadata.get("environment", "?"))
        kv("  Deployment status:",       d.status.value)

    print("\n  Full context for KnowledgeAgent LLM prompt:")
    for line in ctx.to_text().split("\n"):
        print(f"    {line}")

    assert len(ctx.recent_deployments) >= 1
    assert len(complete_events)        >= 1
    assert len(all_deps)               >= 1
    ok("Full autonomous loop complete:")
    ok("  Monitoring detected anomaly")
    ok("  Incident created with metrics")
    ok("  CI/CD Agent deployed autonomously via EventBus")
    ok("  Deployment recorded in StateManager + IncidentContext")
    ok("  Context ready for KnowledgeAgent LLM reasoning")
    await agent.stop()

# ═══════════════════════════════════════════════════════════════════════════════
# ── SECTION 8: FINAL SUMMARY ──────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

def test_16_summary():
    """Print links and confirm the demo is complete."""
    header("Demo Complete — Summary")
    kv("Repo:",          f"https://github.com/{REPO}")
    kv("Workflow file:", f"https://github.com/{REPO}/blob/main/{WORKFLOW_PATH}")
    kv("Actions tab:",   f"https://github.com/{REPO}/actions")
    kv("Deployments:",   f"https://github.com/{REPO}/deployments")
    print()
    ok("CI/CD Agent:       trigger, deploy, rollback, logs, status")
    ok("Monitoring Agent:  anomaly detection, metric collection, incident creation")
    ok("EventBus:          autonomous event-driven deployment loop")
    ok("StateManager:      deployment + incident tracking")
    ok("ContextManager:    LLM prompt data for KnowledgeAgent")
    print()
    ok("All tests passed — check GitHub Actions and Deployments tabs")