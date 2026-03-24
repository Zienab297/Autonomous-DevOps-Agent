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

# ── CI/CD Agent imports ────────────────────────────────────────────────────────
from dotenv import load_dotenv
load_dotenv()

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")

try:
    from agents.cicd_agent.cicd_agent import CICDAgent
    from providers.cicd.github_provider import GitHubProvider
    _CICD_AVAILABLE = bool(GITHUB_TOKEN)
except ImportError:
    _CICD_AVAILABLE = False


async def _run_scaffold():
    project_path = str(Path.cwd())

    config       = load_config()
    orchestrator = Orchestrator()
    orchestrator.register_agent("scaffold_agent", ScaffoldAgent(config))

    # ── Register CICDAgent if token is available ───────────────────────────
    if _CICD_AVAILABLE:
        provider   = GitHubProvider(token=GITHUB_TOKEN, org="")
        cicd_agent = CICDAgent(
            provider    = provider,
            event_bus   = orchestrator.event_bus,
            registry    = orchestrator.registry,
            state       = orchestrator.state_manager,
            ctx_manager = orchestrator.context_manager,
        )
        await cicd_agent.start()
        orchestrator.register_agent("cicd_agent", cicd_agent)
    else:
        if not GITHUB_TOKEN:
            print("  [..] GITHUB_TOKEN not set in .env — CI/CD logs will be skipped")
        else:
            print("  [..] CICDAgent not found — CI/CD logs will be skipped")

    await orchestrator.start()
    await orchestrator.run_scaffold(project_path=project_path)


def main():
    asyncio.run(_run_scaffold())
    AgentController().run()


if __name__ == "__main__":
    main()