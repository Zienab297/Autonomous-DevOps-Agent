"""
main.py
--------
Entry point for the Autonomous DevOps Agent.

Usage:
    python main.py deploy /path/to/project       ← full pipeline
    python main.py monitor                        ← monitoring only (mock)
    python main.py investigate "error message"    ← knowledge agent only

Full pipeline (deploy):
    scaffold → [APPROVAL] → cicd_agent → [APPROVAL] → monitoring
    → incident detected → [APPROVAL] → knowledge_agent
        ├── found in KB  → [APPROVAL: apply RAG solution?]
        └── not in KB    → [APPROVAL: use generated solution?]
    → [APPROVAL] → self_healing_agent → RESOLVED / FAILED
"""

import asyncio
import logging
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

logging.basicConfig(
    level  = logging.INFO,
    format = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt= "%H:%M:%S",
)
logger = logging.getLogger("main")


# ── helpers ───────────────────────────────────────────────────────────────────

def _print_banner():
    print("\n" + "═" * 60)
    print("  AUTONOMOUS DEVOPS AGENT")
    print("  Commands:")
    print("    python main.py deploy /path/to/project")
    print("    python main.py monitor")
    print("    python main.py investigate \"error message\"")
    print("═" * 60 + "\n")


# ── deploy pipeline ───────────────────────────────────────────────────────────

async def run_deploy(project_path: str):
    """
    Full pipeline:
      ScaffoldAgent → CI/CD Agent → MonitoringAgent
      → KnowledgeAgent → SelfHealingAgent
    """
    from core import Orchestrator
    import importlib
    _scaffold_mod = importlib.import_module("agents.scaffold_agent.core_scaffold.scaffold_agent")
    ScaffoldAgent = _scaffold_mod.ScaffoldAgent
    _config_mod = importlib.import_module("agents.scaffold_agent.shared.config")
    ScaffoldConfig = _config_mod.ScaffoldConfig
    from agents.cicd_agent.cicd_agent import CICDAgent
    from agents.self_healing_agent.self_healing_agent import SelfHealingAgent

    orchestrator = Orchestrator()
    await orchestrator.start()

    # ── register scaffold agent ───────────────────────────────────────────
    scaffold_config = ScaffoldConfig()
    scaffold_agent  = ScaffoldAgent(scaffold_config)
    orchestrator.register_agent("scaffold_agent", scaffold_agent)

    # ── register cicd agent ───────────────────────────────────────────────
    cicd_agent = CICDAgent()
    orchestrator.register_agent("cicd_agent", cicd_agent)

    # ── register self-healing agent ───────────────────────────────────────
    self_healing_agent = SelfHealingAgent()
    orchestrator.register_agent("self_healing_agent", self_healing_agent)

    # ── start monitoring agent (already auto-registered by Orchestrator) ──
    await orchestrator.start_monitoring_agent()

    # ── print dashboard ───────────────────────────────────────────────────
    orchestrator.print_dashboard(f"Starting deploy pipeline for: {project_path}")

    # ── kick off scaffold (everything flows from here via event bus) ──────
    await orchestrator.run_scaffold(project_path)

    # ── keep running until pipeline finishes ─────────────────────────────
    # The orchestrator drives everything through events + approvals.
    # We just wait here; Ctrl-C to abort at any approval prompt.
    try:
        while orchestrator._dashboard["stage"] not in ("done",):
            await asyncio.sleep(1)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\n  Pipeline interrupted by user.\n")

    await orchestrator.stop()
    print("\n  Pipeline complete.\n")


# ── monitor only (mock, for testing) ─────────────────────────────────────────

async def run_monitor():
    """
    Monitoring-only mode: injects a mock anomaly and runs the full
    incident → knowledge → self-healing flow without scaffold/cicd.
    Useful for testing the agent pipeline end-to-end.
    """
    from core import Orchestrator
    from agents.monitoring_agent import MonitoringAgent, MonitoringConfig, MockCollector
    from agents.self_healing_agent.self_healing_agent import SelfHealingAgent

    orchestrator = Orchestrator()
    await orchestrator.start()

    # ── register self-healing agent ───────────────────────────────────────
    self_healing_agent = SelfHealingAgent()
    orchestrator.register_agent("self_healing_agent", self_healing_agent)

    # ── build monitoring agent with mock collector ────────────────────────
    collector = MockCollector(seed=42)
    config = MonitoringConfig(
        services          = ["auth-api", "payments-api"],
        poll_interval     = 5.0,
        collector_backend = "mock",
    )
    monitoring_agent = MonitoringAgent(
        event_bus       = orchestrator.event_bus,
        registry        = orchestrator.registry,
        config          = config,
        collector       = collector,
        context_manager = orchestrator.context_manager,
        state_manager   = orchestrator.state_manager,
    )
    orchestrator.register_agent("monitoring_agent", monitoring_agent)

    orchestrator.print_dashboard("Monitoring mode — injecting mock anomaly")

    await monitoring_agent.start()

    logger.info("Waiting for healthy baseline poll...")
    await asyncio.sleep(6)

    logger.info(">>> Injecting CRITICAL error_rate anomaly on auth-api <<<")
    collector.inject_anomaly("auth-api", "error_rate", value=0.55)

    # Wait for incident → knowledge → self-healing flow to complete
    try:
        timeout = 0
        while orchestrator._dashboard["stage"] not in ("done",) and timeout < 300:
            await asyncio.sleep(1)
            timeout += 1
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\n  Interrupted.\n")

    await monitoring_agent.stop()
    await orchestrator.stop()
    print("\n  Monitor run complete.\n")


# ── investigate only ──────────────────────────────────────────────────────────

async def run_investigate(error_message: str):
    """
    Run just the KnowledgeAgent against an error message.
    Useful for testing RAG + self-healing without full pipeline.
    """
    from core import Orchestrator
    from core.models import Incident, IncidentSeverity
    from agents.self_healing_agent.self_healing_agent import SelfHealingAgent
    import uuid

    orchestrator = Orchestrator()
    await orchestrator.start()

    self_healing_agent = SelfHealingAgent()
    orchestrator.register_agent("self_healing_agent", self_healing_agent)

    orchestrator.print_dashboard(f"Investigating: {error_message[:60]}...")

    # Build a minimal incident and hand it to the orchestrator
    incident = Incident(
        incident_id = str(uuid.uuid4()),
        service     = "manual-investigate",
        severity    = IncidentSeverity.HIGH,
        description = error_message,
        metrics     = [],
        logs        = [],
        metadata    = {"llm_analysis": {"files_to_fix": []}},
    )

    await orchestrator.handle_incident(incident)

    # Wait for flow to finish
    try:
        timeout = 0
        while orchestrator._dashboard["stage"] not in ("done",) and timeout < 300:
            await asyncio.sleep(1)
            timeout += 1
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\n  Interrupted.\n")

    await orchestrator.stop()
    print("\n  Investigation complete.\n")


# ── CLI dispatcher ────────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]

    if not args:
        _print_banner()
        sys.exit(0)

    command = args[0].lower()

    if command == "deploy":
        if len(args) < 2:
            print("\n  Usage: python main.py deploy /path/to/project\n")
            sys.exit(1)
        project_path = args[1]
        if not os.path.isdir(project_path):
            print(f"\n  Error: '{project_path}' is not a valid directory.\n")
            sys.exit(1)
        asyncio.run(run_deploy(project_path))

    elif command == "monitor":
        asyncio.run(run_monitor())

    elif command == "investigate":
        if len(args) < 2:
            print('\n  Usage: python main.py investigate "error message"\n')
            sys.exit(1)
        error_message = " ".join(args[1:])
        asyncio.run(run_investigate(error_message))

    else:
        print(f"\n  Unknown command: '{command}'")
        _print_banner()
        sys.exit(1)


if __name__ == "__main__":
    main()