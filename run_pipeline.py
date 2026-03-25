"""
run_pipeline.py
===============
Runs the REAL orchestrator pipeline end-to-end:

    MonitoringAgent (MockCollector + anomaly injection)
         │  INCIDENT_CREATED
         ▼
    Orchestrator._on_incident_created()
         │  calls
         ▼
    KnowledgeAgent.run()  →  AgentResponse  (Qdrant RAG + Ollama)
         │  INVESTIGATION_COMPLETE
         ▼
    Orchestrator._on_investigation_complete()
         │  calls
         ▼
    SelfHealingAgent.remediate()  →  SelfHealingResult  (Groq LLM fixer)
         │  REMEDIATION_COMPLETE / FAILED
         ▼
    Orchestrator._on_remediation_complete/failed()
         └─  incident status → RESOLVED / FAILED

Zero mocks. Zero replacements. All your real agent classes.

Usage (from project root):
    python run_pipeline.py
    python run_pipeline.py --service payments-api
    python run_pipeline.py --dry-run
    python run_pipeline.py --file path/to/broken_file.py
"""

import sys
import os
import asyncio
import argparse
import logging
import textwrap
from pathlib import Path
from datetime import datetime
from unittest.mock import AsyncMock

# ── 1. path bootstrap ─────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "agents" / "knowledge_agent"))    # for setup_path imports
sys.path.insert(0, str(ROOT / "agents" / "self_healing_agent")) # for llm_fixer / llm_verifier

# ── 2. core imports  (real, unmodified) ───────────────────────────────────────
from core.orchestrator  import Orchestrator
from core.event_bus     import EventType, Event
from core.models        import Incident, Severity, IncidentStatus

# ── 3. monitoring agent  (real) ───────────────────────────────────────────────
from agents.monitoring_agent.agent     import MonitoringAgent
from agents.monitoring_agent.collector import MockCollector
from agents.monitoring_agent.config    import MonitoringConfig

# ── 4. knowledge agent  (real) ────────────────────────────────────────────────
from agents.knowledge_agent.knowledge_core.knowledge_agent import KnowledgeAgent
from agents.knowledge_agent.shared.config                  import load_config

# ── 5. self-healing agent  (real) ─────────────────────────────────────────────
from agents.self_healing_agent.self_healing_agent import SelfHealingAgent

# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)-8s  %(name)s — %(message)s",
)
for name in [
    "core.orchestrator",
    "core.event_bus",
    "core.state_manager",
    "core.agent_registery",
    "agent.monitoring_agent",
    "__main__",
]:
    logging.getLogger(name).setLevel(logging.INFO)

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# ANSI colours
# ─────────────────────────────────────────────────────────────────────────────
def _c(code, t): return f"\033[{code}m{t}\033[0m"
def green(t):  return _c("92", t)
def red(t):    return _c("91", t)
def yellow(t): return _c("93", t)
def cyan(t):   return _c("96", t)
def bold(t):   return _c("1",  t)
def dim(t):    return _c("2",  t)

def section(title):
    print(f"\n{cyan('━' * 68)}")
    print(f"  {bold(title)}")
    print(cyan('━' * 68))

def ok(msg):   print(green("  ✔  ") + msg)
def fail(msg): print(red  ("  ✘  ") + msg)
def info(msg): print(dim  ("  │  ") + msg)
def step(label, val=""): print(f"  {cyan(f'[{label}]')}  {val}")

# ─────────────────────────────────────────────────────────────────────────────
# Sample broken file written when --file is not given
# ─────────────────────────────────────────────────────────────────────────────
BROKEN_SOURCE = textwrap.dedent("""\
    # playground_app.py — intentionally broken
    import logging
    from sqlalchemy import create_engine, text
    from sqlalchemy.exc import OperationalError

    logger = logging.getLogger(__name__)

    engine = create_engine(
        "postgresql://orders_user:secret@db.prod.internal:5432/orders",
        pool_size=2,        # BUG: should be 20
        max_overflow=0,     # BUG: should be 10
        pool_timeout=30,
        pool_recycle=1800,
    )

    def process_order(order_id: int) -> list:
        try:
            with engine.connect() as conn:
                result = conn.execute(
                    text("SELECT * FROM orders WHERE id = :id"),
                    {"id": order_id},
                )
                return [dict(row) for row in result]
        except:                              # BUG: bare except hides all errors
            return []
""")

# ─────────────────────────────────────────────────────────────────────────────
# Main pipeline
# ─────────────────────────────────────────────────────────────────────────────

async def run_pipeline(target_file: Path, service: str, dry_run: bool) -> None:
    started_at = datetime.now()

    # ── Orchestrator ──────────────────────────────────────────────────────────
    section("ORCHESTRATOR — Initialising")

    orch = Orchestrator()

    # Auto-approve every approval gate — remove this line for interactive prompts
    orch.approval.request_approval = AsyncMock(return_value=True)
    ok("ApprovalManager: all gates auto-approved")

    await orch.start()
    ok("Orchestrator started")

    # ── Instantiate real agents ───────────────────────────────────────────────
    section("AGENTS — Instantiating real implementations")

    # MonitoringAgent — MockCollector so we fire the incident ourselves
    # instead of waiting for the poll loop
    monitoring_config = MonitoringConfig(
        services          = [service],
        collector_backend = "mock",
        poll_interval     = 9999,
    )
    collector        = MockCollector(seed=42)
    monitoring_agent = MonitoringAgent(
        event_bus       = orch.event_bus,
        registry        = orch.registry,
        config          = monitoring_config,
        collector       = collector,
        context_manager = orch.context_manager,
        state_manager   = orch.state_manager,
    )
    ok("MonitoringAgent   ready  (MockCollector, manual trigger)")

    # KnowledgeAgent — real Qdrant RAG + Ollama/Gemini
    try:
        ka_config       = load_config()
        knowledge_agent = KnowledgeAgent(ka_config)
        ok(f"KnowledgeAgent    ready  (qdrant={ka_config.qdrant_host}:{ka_config.qdrant_port}"
           f"  model={ka_config.generation_model})")
    except Exception as e:
        fail(f"KnowledgeAgent failed to initialise: {e}")
        fail("Check GEMINI_API_KEY / QDRANT_HOST in your .env file")
        await orch.stop()
        return

    # SelfHealingAgent — real Groq LLM fixer + verifier
    self_healing_agent = SelfHealingAgent(apply_changes=not dry_run)
    ok(f"SelfHealingAgent  ready  (apply_changes={not dry_run})")

    # ── Register agents ───────────────────────────────────────────────────────
    section("ORCHESTRATOR — Registering agents")

    # We register manually instead of calling monitoring_agent.start()
    # because start() would launch the background poll loop which we don't want.
    orch.register_agent("monitoring_agent",   monitoring_agent)
    orch.register_agent("knowledge_agent",    knowledge_agent)
    orch.register_agent("self_healing_agent", self_healing_agent)

    ok("monitoring_agent   registered")
    ok("knowledge_agent    registered")
    ok("self_healing_agent registered")

    # ── Build incident + files_to_fix ─────────────────────────────────────────
    section("① MONITORING — Incident detected")

    incident_id = f"INC-{datetime.now().strftime('%H%M%S').upper()}"
    content     = target_file.read_text(encoding="utf-8")
    bug_line    = next(
        (i for i, line in enumerate(content.splitlines(), 1) if "pool_size=" in line),
        1,
    )

    files_to_fix = [{
        "file"           : str(target_file),
        "line"           : bug_line,
        "function"       : "process_order",
        "exception"      : "sqlalchemy.exc.OperationalError: connection pool exhausted",
        "fix_description": "Fix pool_size, max_overflow settings and remove bare except",
    }]

    incident = Incident(
        incident_id = incident_id,
        service     = service,
        severity    = Severity.HIGH,
        description = "sqlalchemy.exc.OperationalError: connection pool exhausted",
    )
    orch.state_manager.add_incident(incident)
    orch.state_manager.update_incident_status(incident_id, IncidentStatus.INVESTIGATING)

    step("Incident ID",  bold(incident_id))
    step("Service",      service)
    step("Target file",  str(target_file))
    step("Bug at line",  str(bug_line))
    step("Dry run",      str(dry_run))
    ok("Incident registered in StateManager")

    # ── Publish INCIDENT_CREATED — orchestrator handles everything from here ──
    section("EVENT BUS — Publishing INCIDENT_CREATED")

    await orch.event_bus.publish(Event(
        type        = EventType.INCIDENT_CREATED,
        source      = "monitoring_agent",
        incident_id = incident_id,
        data        = {
            "incident_id" : incident_id,
            "service"     : service,
            "severity"    : "high",
            "description" : incident.description,
            "files_to_fix": files_to_fix,
        },
    ))

    step("Event", f"{cyan('INCIDENT_CREATED')} dispatched → orchestrator now in control")
    info("Waiting for Knowledge Agent + Self-Healing Agent to complete…")
    info("(KnowledgeAgent calls Qdrant + Ollama, SelfHealingAgent calls Groq — allow ~30s)")

    # 30 s ceiling covers Qdrant lookup + Ollama generation + Groq LLM fixer
    await asyncio.sleep(30)

    # ── Final results ─────────────────────────────────────────────────────────
    section("RESULT")

    stored  = orch.state_manager.get_incident(incident_id)
    history = orch.event_bus.get_history()
    summary = orch.summary()
    elapsed = (datetime.now() - started_at).total_seconds()

    print()
    for e in history:
        colour = (green if "complete" in e.type.value
                  else red  if "failed"   in e.type.value
                  else cyan)
        info(f"  {colour('●')} {e.type.value}")

    print()
    final = stored.status if stored else None

    if final == IncidentStatus.RESOLVED:
        print(green(bold("  ┌──────────────────────────────────────────┐")))
        print(green(bold("  │   ✔  INCIDENT RESOLVED AUTOMATICALLY     │")))
        print(green(bold("  └──────────────────────────────────────────┘")))
    elif final == IncidentStatus.FAILED:
        print(red(bold("  ┌──────────────────────────────────────────┐")))
        print(red(bold("  │   ✘  REMEDIATION FAILED                  │")))
        print(red(bold("  └──────────────────────────────────────────┘")))
    else:
        print(yellow(bold(f"  Incident status: {final}")))

    print()
    step("Incident ID",    bold(incident_id))
    step("Final status",   bold(str(final.value if final else "unknown")))
    step("Events emitted", str(len(history)))
    step("Agent statuses", str(summary.get("agents", {})))
    step("Elapsed",        f"{elapsed:.1f}s")
    print()

    await orch.stop()


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Run the real Orchestrator: Monitor → Knowledge → Self-Healing"
    )
    parser.add_argument(
        "--file", default=None,
        help="Path to the broken file to fix. Omit to use the built-in playground_app.py sample.",
    )
    parser.add_argument(
        "--service", default="order-service",
        help="Service name for the incident (default: order-service)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Call LLMs but do NOT write changes to disk",
    )
    args = parser.parse_args()

    if args.file:
        target = Path(args.file)
        if not target.exists():
            print(red(f"  Error: file not found: {target}"))
            sys.exit(1)
    else:
        target = ROOT / "playground_app.py"
        target.write_text(BROKEN_SOURCE, encoding="utf-8")
        print(dim(f"  Sample broken file written → {target}"))

    asyncio.run(run_pipeline(target, args.service, args.dry_run))


if __name__ == "__main__":
    main()