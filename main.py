"""
main.py
--------
Entry point for the DevOps Agent system.

Wires the Orchestrator, EventBus, and MonitoringAgent together,
injects a test anomaly, and runs for two poll cycles to demonstrate
the full INCIDENT_CREATED event flow.

Run:
    python main.py
"""

import asyncio
import logging
import sys
import os

# Make sure imports resolve from the project root
sys.path.insert(0, os.path.dirname(__file__))

from core import Orchestrator
from agents.monitoring_agent import MonitoringAgent, MonitoringConfig, MockCollector

logging.basicConfig(
    level  = logging.INFO,
    format = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt= "%H:%M:%S",
)
logger = logging.getLogger("main")


async def main():
    # ── 1. Orchestrator (owns EventBus, StateManager, ContextManager, Registry)
    orchestrator = Orchestrator()
    await orchestrator.start()

    # ── 2. Build a mock collector so we can inject anomalies on demand
    collector = MockCollector(seed=42)

    # ── 3. MonitoringAgent (fast poll for demo: 5 seconds)
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
    )

    # Register with orchestrator so summary() includes it
    orchestrator.register_agent("monitoring_agent", monitoring_agent)

    logger.info("=" * 60)
    logger.info("DevOps Agent — Monitoring Demo")
    logger.info("=" * 60)

    # ── 4. Start the monitoring agent
    await monitoring_agent.start()

    # ── 5. Let the first (healthy) poll run
    logger.info("Waiting for first poll (healthy baseline)...")
    await asyncio.sleep(6)

    # ── 6. Inject a critical anomaly on auth-api
    logger.info("")
    logger.info(">>> Injecting CRITICAL error_rate anomaly on auth-api <<<")
    collector.inject_anomaly("auth-api", "error_rate", value=0.55)

    # ── 7. Wait for the anomaly to be detected and event published
    await asyncio.sleep(6)

    # ── 8. Show system summary
    logger.info("")
    logger.info("System summary: %s", orchestrator.summary())

    # ── 9. Resolve the anomaly — next poll should clear it
    logger.info("")
    logger.info(">>> Clearing anomaly on auth-api <<<")
    collector.clear_anomaly("auth-api")
    await asyncio.sleep(6)

    # ── 10. Shutdown
    logger.info("Stopping monitoring agent...")
    await monitoring_agent.stop()
    await orchestrator.stop()
    logger.info("Done.")


if __name__ == "__main__":
    asyncio.run(main())