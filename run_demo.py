"""
run_demo.py
============
End-to-end demo of the CI/CD Agent against Zienab297/test-devops.

What this script does, in order:
  1. Loads GITHUB_TOKEN from .env
  2. Commits deploy.yml to the repo via GitHub Contents API
  3. Boots the full core stack (Orchestrator, EventBus, StateManager, etc.)
  4. Starts the CICDAgent
  5. Runs every operation and prints results clearly:
       - health_check
       - trigger_pipeline  (fires deploy.yml, waits for completion)
       - collect_logs      (fetches step output)
       - deploy            (creates GitHub Deployment record)
       - rollback          (creates rolled-back deployment)
       - event-driven flow (publishes DEPLOYMENT_STARTED on bus)
       - incident flow     (full incident → context → deploy → state)

Run:
    python run_demo.py
"""
from __future__ import annotations

import asyncio
import base64
import os
import sys
import time
import textwrap
from datetime import datetime

import aiohttp
from dotenv import load_dotenv

load_dotenv()

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
if not GITHUB_TOKEN:
    print("ERROR: GITHUB_TOKEN not set in .env")
    sys.exit(1)

REPO   = "Zienab297/test-devops"
BRANCH = "main"
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

# ── print helpers ─────────────────────────────────────────────────────────────

SEP  = "=" * 64
SEP2 = "-" * 64

def header(title: str) -> None:
    print(f"\n{SEP}")
    print(f"  {title}")
    print(SEP)

def section(title: str) -> None:
    print(f"\n{SEP2}")
    print(f"  {title}")
    print(SEP2)

def ok(msg: str) -> None:
    print(f"  [OK]  {msg}")

def info(msg: str) -> None:
    print(f"  [..] {msg}")

def fail(msg: str) -> None:
    print(f"  [FAIL] {msg}")

def kv(key: str, value) -> None:
    print(f"  {key:<20} {value}")

# ── step 1: commit deploy.yml to repo ────────────────────────────────────────

async def ensure_workflow_file() -> None:
    section("Step 1 — Commit deploy.yml to repo")

    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    url = f"https://api.github.com/repos/{REPO}/contents/{WORKFLOW_PATH}"

    async with aiohttp.ClientSession(headers=headers) as s:
        # Check if file already exists (need its SHA to update)
        sha = None
        async with s.get(url) as resp:
            if resp.status == 200:
                data = await resp.json()
                sha = data.get("sha")
                info(f"deploy.yml already exists (sha={sha[:8]}...) — updating")
            else:
                info("deploy.yml not found — creating it now")

        content_b64 = base64.b64encode(WORKFLOW_CONTENT.encode()).decode()
        body = {
            "message": "Add deploy.yml via DevOps Agent SDK demo",
            "content": content_b64,
            "branch":  BRANCH,
        }
        if sha:
            body["sha"] = sha

        async with s.put(url, json=body) as resp:
            if resp.status in (200, 201):
                data = await resp.json()
                commit_sha = data["commit"]["sha"]
                ok(f"deploy.yml committed — commit {commit_sha[:8]}")
                ok(f"URL: https://github.com/{REPO}/blob/main/{WORKFLOW_PATH}")
            else:
                text = await resp.text()
                fail(f"Could not commit deploy.yml [{resp.status}]: {text}")
                print("\nThis usually means the token needs 'Contents: write' permission.")
                sys.exit(1)

# ── step 2: boot core stack ───────────────────────────────────────────────────

def boot_stack():
    section("Step 2 — Boot core stack")

    # Add project root to path
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

    from core.orchestrator        import Orchestrator
    from core.event_bus           import Event, EventType
    from core.models              import Incident, Severity
    from agents.cicd_agent.cicd_agent   import CICDAgent
    from providers.cicd.github_provider import GitHubProvider

    orch     = Orchestrator()
    provider = GitHubProvider(token=GITHUB_TOKEN, org="Zienab297")
    agent    = CICDAgent(
        provider    = provider,
        event_bus   = orch.event_bus,
        registry    = orch.registry,
        state       = orch.state_manager,
        ctx_manager = orch.context_manager,
    )

    ok("Orchestrator created")
    ok("EventBus ready")
    ok("StateManager ready")
    ok("ContextManager ready")
    ok("GitHubProvider created")
    ok("CICDAgent created")

    return orch, agent, provider, Event, EventType, Incident, Severity

# ── wait helper ───────────────────────────────────────────────────────────────

async def wait_for_run(agent, run_id: str, timeout: int = 120) -> object:
    info(f"Waiting for run {run_id} to complete (timeout={timeout}s) ...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = await agent.get_pipeline_status(run_id, REPO)
        if run.status in ("success", "failed", "cancelled"):
            return run
        elapsed = int(timeout - (deadline - time.time()))
        info(f"  still running... ({elapsed}s elapsed, status={run.status})")
        await asyncio.sleep(8)
    raise TimeoutError(f"Run {run_id} did not finish within {timeout}s")

# ── main demo ─────────────────────────────────────────────────────────────────

async def main() -> None:
    header("DevOps Agent SDK — Live Demo on Zienab297/test-devops")
    kv("Repo:",   f"https://github.com/{REPO}")
    kv("Branch:", BRANCH)
    kv("Time:",   datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"))

    # Step 1 — commit the workflow file
    await ensure_workflow_file()

    # Brief pause so GitHub's API registers the new file
    info("Waiting 3s for GitHub to register the new file ...")
    await asyncio.sleep(3)

    # Step 2 — boot stack
    orch, agent, provider, Event, EventType, Incident, Severity = boot_stack()

    # ── step 3: start agent ──────────────────────────────────────────────────
    section("Step 3 — Start CICDAgent")
    await agent.start()
    ok(f"Agent state   : {agent.state.value}")
    ok(f"Agent id      : {agent.agent_id}")
    ok(f"Registered    : {orch.registry.is_registered('cicd_agent')}")
    ok(f"Bus subs (DEPLOYMENT_STARTED) : "
       f"{orch.event_bus.get_subscribers_count(EventType.DEPLOYMENT_STARTED)}")

    # ── step 4: health check ─────────────────────────────────────────────────
    section("Step 4 — Health check")
    result = await agent.health_check()
    kv("Provider:", result["provider"])
    kv("Healthy:",  result["healthy"])
    kv("State:",    result["state"])
    if result["healthy"]:
        ok("GitHub API is reachable with this token")
    else:
        fail("Token cannot reach GitHub API — check permissions")
        sys.exit(1)

    # ── step 5: trigger pipeline ─────────────────────────────────────────────
    section("Step 5 — Trigger pipeline (workflow_dispatch)")
    info(f"Firing {WORKFLOW_PATH} on {REPO} ...")

    run = await agent.trigger_pipeline(
        repo     = REPO,
        branch   = BRANCH,
        workflow = "deploy.yml",
        inputs   = {
            "environment":  "staging",
            "version":      BRANCH,
            "triggered_by": "devops-agent-sdk-demo",
        },
    )
    kv("Run ID:",    run.id)
    kv("Status:",    run.status)
    kv("Branch:",    run.branch)
    kv("URL:",       run.url)
    ok("Pipeline triggered successfully")

    # ── step 6: wait for pipeline ────────────────────────────────────────────
    section("Step 6 — Wait for pipeline to complete")
    final = await wait_for_run(agent, run.id, timeout=120)
    kv("Final status:",  final.status)
    kv("Finished at:",   str(final.finished_at))
    if final.status == "success":
        ok("Pipeline completed successfully")
    else:
        fail(f"Pipeline ended with status: {final.status}")
        fail(f"Check: {run.url}")

    # ── step 7: collect logs ─────────────────────────────────────────────────
    section("Step 7 — Collect deployment logs")
    logs = await agent.collect_deployment_logs(run.id, REPO)
    kv("Log lines:", len(logs))
    print()
    for line in logs:
        print(f"  {line}")
    ok("Logs collected successfully")

    # ── step 8: deploy ───────────────────────────────────────────────────────
    section("Step 8 — Create GitHub Deployment record")
    info("Calling GitHub Deployments API ...")

    dep_events: list = []
    async def capture_dep(e): dep_events.append(e)
    orch.event_bus.subscribe(EventType.DEPLOYMENT_STARTED,  capture_dep)
    orch.event_bus.subscribe(EventType.DEPLOYMENT_COMPLETE, capture_dep)

    dep = await agent.deploy(
        service     = REPO,
        branch      = BRANCH,
        environment = "staging",
    )
    kv("Deployment ID:",  dep.deployment_id)
    kv("Service:",        dep.service)
    kv("Branch:",         dep.branch)
    kv("Status:",         dep.status.value)
    kv("Pipeline URL:",   dep.pipeline_url)
    ok("Deployment record created on GitHub")

    print(f"\n  Events fired on EventBus:")
    for e in dep_events:
        print(f"    [{e.type.value}] source={e.source} "
              f"status={e.data.get('status', '?')}")

    stored = orch.state_manager.get_deployment(dep.deployment_id)
    kv("\n  In StateManager:", stored.deployment_id if stored else "NOT FOUND")
    if stored:
        ok("Deployment stored in StateManager")

    # ── step 9: rollback ─────────────────────────────────────────────────────
    section("Step 9 — Rollback to previous version")
    info("Simulating rollback to SHA/tag 'main' ...")

    rb_events: list = []
    async def capture_rb(e): rb_events.append(e)
    orch.event_bus.subscribe(EventType.ROLLBACK_TRIGGERED,  capture_rb)

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
    ok("Rollback complete")

    print(f"\n  Events fired on EventBus:")
    for e in rb_events:
        print(f"    [{e.type.value}] source={e.source}")

    rb_stored = orch.state_manager.get_deployment(result.deployment_id)
    if rb_stored:
        kv("\n  In StateManager:", rb_stored.status.value)
        ok("Rollback deployment stored in StateManager")

    # ── step 10: event-driven deploy ─────────────────────────────────────────
    section("Step 10 — Event-driven deploy (publish on EventBus)")
    info("Publishing DEPLOYMENT_STARTED — agent will react autonomously ...")

    bus_events: list = []
    async def capture_bus(e): bus_events.append(e)
    orch.event_bus.subscribe(EventType.DEPLOYMENT_COMPLETE, capture_bus)

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

    kv("DEPLOYMENT_COMPLETE events received:", len(bus_events))
    if bus_events:
        e = bus_events[0]
        kv("  service:", e.data.get("service"))
        kv("  status:",  e.data.get("status"))
        ok("Agent responded to bus event autonomously")

    # ── step 11: full incident flow ──────────────────────────────────────────
    section("Step 11 — Full incident flow")
    info("Creating incident → context → triggering deploy via bus ...")

    incident = Incident(
        service     = REPO,
        severity    = Severity.HIGH,
        description = "Simulated error rate spike on test-devops",
    )
    orch.state_manager.add_incident(incident)
    orch.context_manager.create_context(incident)

    kv("Incident ID:",   incident.incident_id)
    kv("Service:",       incident.service)
    kv("Severity:",      incident.severity.value)
    kv("Description:",   incident.description)

    inc_events: list = []
    async def capture_inc(e): inc_events.append(e)
    orch.event_bus.subscribe(EventType.DEPLOYMENT_COMPLETE, capture_inc)

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

    ctx = orch.context_manager.get_context(incident.incident_id)
    kv("\n  Deployments in context:", len(ctx.recent_deployments))
    if ctx.recent_deployments:
        d = ctx.recent_deployments[0]
        kv("  Deployment ID:", d.deployment_id)
        kv("  Status:",        d.status.value)
        ok("Deployment attached to IncidentContext")

    all_deps = orch.state_manager.get_deployments_for_service(REPO)
    kv("  Total deployments in StateManager:", len(all_deps))

    print("\n  Context text (sent to KnowledgeAgent for LLM prompt):")
    ctx_text = ctx.to_text()
    for line in ctx_text.split("\n")[:20]:
        print(f"    {line}")

    # ── step 12: stop agent ──────────────────────────────────────────────────
    section("Step 12 — Stop CICDAgent")
    await agent.stop()
    kv("Agent state:", agent.state.value)
    kv("Still registered:", orch.registry.is_registered("cicd_agent"))
    ok("Agent stopped cleanly")

    await provider.close()

    # ── final summary ────────────────────────────────────────────────────────
    header("Demo Complete — Summary")
    kv("Repo:",                    f"https://github.com/{REPO}")
    kv("Workflow file:",           f"https://github.com/{REPO}/blob/main/{WORKFLOW_PATH}")
    kv("Actions tab:",             f"https://github.com/{REPO}/actions")
    kv("Deployments tab:",         f"https://github.com/{REPO}/deployments")
    kv("Pipeline run:",            run.url)
    kv("Pipeline final status:",   final.status)
    kv("Total deployments made:",  len(orch.state_manager.get_deployments_for_service(REPO)))
    kv("Total bus events fired:",  len(orch.event_bus.get_history()))
    print()
    ok("All operations completed against Zienab297/test-devops")
    ok("Check the Actions and Deployments tabs on GitHub to see the results")


if __name__ == "__main__":
    asyncio.run(main())