"""
main.py
--------
Entry point for the DevOps Agent system.

Starts the Orchestrator with:
  - MonitoringAgent running continuously (never stops)
  - SelfHealingAgent registered and ready
  - KnowledgeAgent registered and ready

Incident routing (automatic, no code changes needed):
  • syntax error   → SelfHealingAgent fixes file(s) directly
  • runtime/import → User instructions printed + KnowledgeAgent investigates
                     → SelfHealingAgent applies the generated fix

Run:
    python main.py
    python main.py --no-dashboard     (disable live terminal UI)
    python main.py --auto-approve     (skip all approval prompts)
"""

import asyncio
import logging
import signal
import sys
import os
import argparse

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "agents", "self_healing_agent"))

from core import Orchestrator
from agents.monitoring_agent import MonitoringAgent, MonitoringConfig, MockCollector

logging.basicConfig(
    level  = logging.INFO,
    format = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt= "%H:%M:%S",
)
logger = logging.getLogger("main")


async def main(args: argparse.Namespace) -> None:

    # ── 1. Orchestrator
    orchestrator = Orchestrator()
    await orchestrator.start()

    # ── 2. Auto-approve gate (useful in CI or dev mode)
    if args.auto_approve:
        from unittest.mock import AsyncMock
        orchestrator.approval.request_approval = AsyncMock(return_value=True)
        logger.info("[main] All approval gates auto-approved")

    # ── 3. MonitoringAgent — file backend watches the logs/ directory
    #       Use collector_backend="mock" and inject anomalies for development.
    config = MonitoringConfig(
        services          = ["auth-api", "payments-api"],
        poll_interval     = 30.0,           # check every 30 s
        collector_backend = "file",         # reads real log files
        log_dir           = "logs",
    )
    monitoring_agent = MonitoringAgent(
        event_bus       = orchestrator.event_bus,
        registry        = orchestrator.registry,
        config          = config,
        context_manager = orchestrator.context_manager,
        state_manager   = orchestrator.state_manager,
        live_dashboard  = not args.no_dashboard,
    )

    # Register externally so the orchestrator's auto-registration is skipped
    orchestrator.register_agent("monitoring_agent", monitoring_agent)

    logger.info("=" * 60)
    logger.info("DevOps Agent System")
    logger.info("  monitoring   : continuous (file backend, logs/)")
    logger.info("  self-healing : registered, apply_changes=True")
    logger.info("  routing      : syntax → auto-fix | other → instructions + knowledge")
    logger.info("=" * 60)

    # ── 4. Start monitoring — runs forever in the background
    await orchestrator.start_monitoring_agent()

    # ── 5. Graceful shutdown on Ctrl-C / SIGTERM
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _request_stop(*_):
        logger.info("[main] Shutdown requested")
        loop.call_soon_threadsafe(stop_event.set)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except (NotImplementedError, RuntimeError):
            # Windows doesn't support add_signal_handler for all signals
            pass

    logger.info("[main] System running — press Ctrl-C to stop")
    await stop_event.wait()

    # ── 6. Shutdown
    logger.info("[main] Stopping monitoring agent...")
    await monitoring_agent.stop()
    await orchestrator.stop()
    logger.info("[main] Done.")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Autonomous DevOps Agent — continuous monitoring + self-healing"
    )
    p.add_argument(
        "--no-dashboard",
        action="store_true",
        help="Disable the live terminal dashboard",
    )
    p.add_argument(
        "--auto-approve",
        action="store_true",
        help="Skip all approval prompts (useful in CI / dev mode)",
    )
    return p.parse_args()


if __name__ == "__main__":
    asyncio.run(main(parse_args()))