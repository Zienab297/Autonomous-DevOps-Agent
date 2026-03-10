"""
agent/knowledge_agent.py
------------------------
Main entry point for the Knowledge Agent layer.

Flow:
    error_message
        │
        ▼
    retriever  ──found──▶  AgentResponse (source=knowledge_base)
        │
      not found
        │
        ▼
    llm_generator ──────▶  AgentResponse (source=llm_generated)
        │
        ▼
    → Self-Healing Agent
"""

import re
from qdrant_client import QdrantClient

from core  import AgentResponse, RAGResult, RAGSource, ErrorCategory
from core.config  import Config
from knowledge_core.retriever     import retrieve
from knowledge_core.research_agent import generate_solution


class KnowledgeAgent:

    def __init__(self, config: Config):
        self.config = config
        self.client = QdrantClient(
            host=config.qdrant_host,
            port=config.qdrant_port,
        )

    def run(self, error_message: str) -> AgentResponse:
        """
        Main method — call this with the raw error string.
        Returns AgentResponse ready for the Self-Healing Agent.
        """
        print(f"\n[KnowledgeAgent] Processing: {error_message[:100]}...")

        # ── try knowledge base first ─────────────────────────────────────
        retrieval = retrieve(error_message, self.client, self.config)

        if retrieval.found:
            print(f"[KnowledgeAgent] ✓ Found in knowledge base "
                  f"(score={retrieval.score})")
            entry = retrieval.entry
            return AgentResponse(
                source=RAGSource.KNOWLEDGE_BASE,
                confidence=retrieval.score,
                healing_prompt=entry.get("healing_prompt", ""),
                suggested_commands=self._extract_commands(
                    entry.get("healing_prompt", "")
                ),
                category=ErrorCategory(entry.get("category", "Unknown")),
                action_needed=True,
                rag_result=RAGResult(
                    entry_id=entry.get("id", ""),
                    category=ErrorCategory(entry.get("category", "Unknown")),
                    confidence=retrieval.score,
                    healing_prompt=entry.get("healing_prompt", ""),
                    root_cause=entry.get("root_cause", ""),
                    error_pattern=entry.get("error_pattern", ""),
                ),
            )

        # ── fallback: LLM + web search ───────────────────────────────────
        print(f"[KnowledgeAgent] ✗ Not found (score={retrieval.score}) "
              f"→ calling LLM generator...")
        solution = generate_solution(error_message, self.config)

        return AgentResponse(
            source=RAGSource.LLM_GENERATED,
            confidence=solution.confidence,
            healing_prompt=solution.healing_prompt,
            suggested_commands=self._extract_commands(solution.healing_prompt),
            category=ErrorCategory.UNKNOWN,
            action_needed=True,
        )

    @staticmethod
    def _extract_commands(healing_prompt: str) -> list[str]:
        """
        Pull out shell commands from the healing prompt.
        Looks for lines inside ```bash blocks or starting with known prefixes.
        """
        commands = []

        # extract from ```bash ... ``` blocks
        bash_blocks = re.findall(r"```(?:bash|sh)\n(.*?)```", healing_prompt, re.DOTALL)
        for block in bash_blocks:
            for line in block.splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    commands.append(line)

        # fallback: lines starting with kubectl / docker / helm
        if not commands:
            for line in healing_prompt.splitlines():
                line = line.strip()
                if line.startswith(("kubectl", "docker", "helm", "git")):
                    commands.append(line)

        return commands