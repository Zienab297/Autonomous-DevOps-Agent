"""
knowledge_core/knowledge_agent_adapter.py
-----------------------------------------
Adapter between the Core Orchestrator and the Knowledge Agent.

CHANGES:
  - run() now accepts failed_solutions: List[str] via the `extra` dict.
    These are healing_prompt summaries from previous attempts that were
    verified as FAILED — passed to KnowledgeAgent.run() so the LLM avoids
    repeating them.

  - add_to_kb() NEW — called by the Orchestrator after a second-attempt fix
    passes verification. Embeds the winning solution and upserts it into the
    Qdrant collection so it becomes part of the permanent knowledge base.

IMPORTANT — sys.path note:
    This file must NOT import from bare "shared.*" because other agents
    (scaffold_agent) also have a "shared" package and whichever gets into
    sys.path first wins.  All imports here use the full absolute package
    path: agents.knowledge_agent.shared.*
"""

import sys
import pathlib
import logging
from typing import List, Optional

logger = logging.getLogger(__name__)

# ── Ensure project root is in sys.path ───────────────────────────────────────
_project_root = str(pathlib.Path(__file__).resolve().parents[3])
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

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

    if response.source == RAGSource.KNOWLEDGE_BASE and response.rag_result:
        print()
        print("  -- KB Reference --")
        print(f"  entry_id      : {response.rag_result.entry_id}")
        print(f"  error_pattern : {response.rag_result.error_pattern}")
        print(f"  root_cause    : {response.rag_result.root_cause}")
        print("  ------------------")

    if response.source == RAGSource.LLM_GENERATED and response.web_sources:
        print()
        print("  -- Web Search References --")
        for i, url in enumerate(response.web_sources[:5], 1):
            print(f"  [{i}] {url}")
        print("  ---------------------------")

    if response.suggested_commands:
        print()
        print("  commands:")
        for cmd in response.suggested_commands[:3]:
            print(f"    $ {cmd}")

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
        self._config = load_config()
        self.agent   = KnowledgeAgent(self._config)

    def run(
        self,
        error_message    : str,
        extra            : dict = None,
    ) -> AgentResponse:
        """
        Called by the Orchestrator's _on_incident_created.

        extra dict keys used:
            files_to_fix     : list — passed through for display
            failed_solutions : List[str] — summaries of previously-tried
                               healing_prompts that were verified as FAILED.
                               Passed to KnowledgeAgent so the LLM avoids
                               repeating them.
        """
        extra            = extra or {}
        failed_solutions : List[str] = extra.get("failed_solutions", [])

        print(f"[KnowledgeAgentAdapter] Running for: {error_message[:80]}...")
        if failed_solutions:
            print(
                f"[KnowledgeAgentAdapter] {len(failed_solutions)} failed "
                f"solution(s) excluded from suggestions."
            )

        response: AgentResponse = self.agent.run(
            error_message    = error_message,
            failed_solutions = failed_solutions,
        )
        _print_response(response)
        return response

    async def investigate(self, context) -> object:
        error_message = self._build_error_message(context)
        print(f"[KnowledgeAgentAdapter] Running for: {error_message[:80]}...")
        response: AgentResponse = self.agent.run(error_message)
        _print_response(response)
        return self._to_solution(context.incident.incident_id, response)

    # ── NEW: write successful solution back to the knowledge base ────────────

    def add_to_kb(
        self,
        incident_id    : str,
        root_cause     : str,
        healing_prompt : str,
        commands       : Optional[List[str]] = None,
        tags           : Optional[List[str]] = None,
    ) -> bool:
        """
        Embed the winning solution and upsert it into the Qdrant collection
        so it becomes part of the permanent knowledge base.

        Called by the Orchestrator ONLY after:
            1. A second-or-later attempt fix was applied.
            2. verify_fix() returned PASS for that fix.

        Parameters
        ----------
        incident_id    : Used to generate a unique vector ID (hash-based).
        root_cause     : One-line diagnosis.
        healing_prompt : The full solution narrative that was verified to work.
        commands       : CLI commands from the winning solution (for metadata).
        tags           : Optional extra tags (e.g. ["github-actions", "docker"]).

        Returns
        -------
        True on success, False on any error.
        """
        commands = commands or []
        tags     = tags     or []

        try:
            from qdrant_client import QdrantClient
            from qdrant_client.models import PointStruct
            from sentence_transformers import SentenceTransformer
            import hashlib, time

            client     = QdrantClient(host=self._config.qdrant_host, port=self._config.qdrant_port)
            collection = self._config.collection_name

            # Build the text to embed — same format as the seed documents
            text = (
                f"Auto-discovered fix for incident {incident_id}. "
                f"Root cause: {root_cause}. "
                f"Healing guide: {healing_prompt}"
            )

            # Generate a deterministic integer ID from the incident_id string
            # so duplicate incidents don't create duplicate vectors.
            vector_id = int(hashlib.md5(incident_id.encode()).hexdigest(), 16) % (2 ** 31)

            model  = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
            vector = model.encode(text).tolist()

            point = PointStruct(
                id     = vector_id,
                vector = vector,
                payload = {
                    "text"         : text,
                    "root_cause"   : root_cause,
                    "healing_prompt": healing_prompt,
                    "commands"     : commands,
                    "source"       : f"auto_healed/{incident_id}",
                    "tags"         : tags + ["auto_healed"],
                    "incident_id"  : incident_id,
                },
            )

            client.upsert(collection_name=collection, points=[point])
            logger.info(
                "[KnowledgeAgentAdapter] add_to_kb: upserted vector id=%d "
                "for incident %s into '%s'",
                vector_id, incident_id, collection,
            )
            print(
                f"\n  [Knowledge Base] ✅ New solution for {incident_id} "
                f"saved to knowledge base (vector id={vector_id}).\n"
            )
            return True

        except Exception as e:
            logger.error(
                "[KnowledgeAgentAdapter] add_to_kb failed for %s: %s",
                incident_id, e,
            )
            print(f"\n  [Knowledge Base] ⚠️  Could not save to KB: {e}\n")
            return False

    # ── helpers ──────────────────────────────────────────────────────────────

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