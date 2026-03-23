import sys
import os
import asyncio
from pathlib import Path

# ── paths ──────────────────────────────────────────────────────────────────────
DEVOPS_AGENT_DIR = Path(__file__).resolve().parent
ROOT             = DEVOPS_AGENT_DIR.parent

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEVOPS_AGENT_DIR))
sys.path.insert(0, str(ROOT / "agents" / "scaffold_agent"))

from controllers.agent_controller import AgentController
from agents.scaffold_agent.shared.config import load_config
from agents.scaffold_agent.core_scaffold.scaffold_agent import ScaffoldAgent
from core.orchestrator import Orchestrator
from core.event_bus import EventType


async def _run_scaffold():
    project_path = str(Path.cwd())

    config       = load_config()
    orchestrator = Orchestrator()
    orchestrator.register_agent("scaffold_agent", ScaffoldAgent(config))

    await orchestrator.start()
    await orchestrator.run_scaffold(project_path=project_path)


def main():
    asyncio.run(_run_scaffold())
    AgentController().run()


if __name__ == "__main__":
    main()