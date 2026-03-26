"""
knowledge_core/knowledge_agent_adapter.py
-----------------------------------------
Adapter between the Core Orchestrator and the Knowledge Agent.

IMPORTANT — sys.path note:
    This file must NOT import from bare "shared.*" because other agents
    (scaffold_agent) also have a "shared" package and whichever gets into
    sys.path first wins.  All imports here use the full absolute package
    path: agents.knowledge_agent.shared.*
"""

import sys
import pathlib

# ── Ensure project root is in sys.path (not knowledge_agent root) ─────────
_project_root = str(pathlib.Path(__file__).resolve().parents[3])  # …/Autonomous-DevOps-Agent
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

# ── Absolute imports — never bare "shared.*" ──────────────────────────────
from agents.knowledge_agent.shared.models  import AgentResponse, RAGSource
from agents.knowledge_agent.shared.config  import load_config
from agents.knowledge_agent.knowledge_core.knowledge_agent import KnowledgeAgent


def _print_response(response: AgentResponse):
    """Print Knowledge Agent response — same format as test_knowledge_agent.py"""
    print()
    print("  [Knowledge Agent Response]")
    print(f"  source     : {response.source.value}")
    print(f"  confidence : {response.confidence:.2f}")
    print(f"  category   : {response.category.value}")
    print(f"  action     : {response.action_needed}")

    # KB reference
    if response.source == RAGSource.KNOWLEDGE_BASE and response.rag_result:
        print()
        print("  -- KB Reference --")
        print(f"  entry_id      : {response.rag_result.entry_id}")
        print(f"  error_pattern : {response.rag_result.error_pattern}")
        print(f"  root_cause    : {response.rag_result.root_cause}")
        print("  ------------------")

    # web search references
    if response.source == RAGSource.LLM_GENERATED and response.web_sources:
        print()
        print("  -- Web Search References --")
        for i, url in enumerate(response.web_sources[:5], 1):
            print(f"  [{i}] {url}")
        print("  ---------------------------")

    # commands
    if response.suggested_commands:
        print()
        print("  commands:")
        for cmd in response.suggested_commands[:3]:
            print(f"    $ {cmd}")

    # solution
    print()
    print("  -- Solution --")
    print("  " + response.healing_prompt.replace("\n", "\n  "))
    print("  --------------")
    print()


class KnowledgeAgentAdapter:
    """
    Drop-in replacement for DummyKnowledgeAgent in the Orchestrator.

    Usage:
        orchestrator.register_agent("knowledge_agent", KnowledgeAgentAdapter())
    """

    def __init__(self):
        self.agent = KnowledgeAgent(load_config())

    def run(self, error_message: str, extra: dict = None) -> AgentResponse:
        """
        Called by the Orchestrator's _on_incident_created.
        Runs the Knowledge Agent and returns AgentResponse directly.
        """
        extra = extra or {}
        print(f"[KnowledgeAgentAdapter] Running for: {error_message[:80]}...")
        response: AgentResponse = self.agent.run(error_message)
        _print_response(response)
        return response

    async def investigate(self, context) -> object:
        error_message = self._build_error_message(context)
        print(f"[KnowledgeAgentAdapter] Running for: {error_message[:80]}...")
        response: AgentResponse = self.agent.run(error_message)
        _print_response(response)
        return self._to_solution(context.incident.incident_id, response)

    @staticmethod
    def _build_error_message(context) -> str:
        parts = [context.incident.description]
        if context.logs:
            error_logs = [
                log.message for log in context.logs
                if getattr(log, "level", "").upper() in ("ERROR", "CRITICAL")
            ][:3]
            if error_logs:
                parts.append("Logs: " + " | ".join(error_logs))
        return "\n".join(parts)

    @staticmethod
    def _to_solution(incident_id: str, response: AgentResponse) -> object:
        from core.models import Solution
        return Solution(
            incident_id=incident_id,
            root_cause=response.healing_prompt,
            healing_prompt=response.healing_prompt,
            confidence=response.confidence,
            suggested_commands=response.suggested_commands,
        )