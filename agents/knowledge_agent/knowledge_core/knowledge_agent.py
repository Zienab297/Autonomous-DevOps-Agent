"""
knowledge_core/knowledge_agent.py
----------------------------------
Main entry point for the Knowledge Agent layer.

Flow:
    error_message
        │
        ▼
    Knowledge Graph enriches query
        │
        ▼
    retriever  ──found──▶  Ollama formats KB answer ▶  AgentResponse (source=knowledge_base)
        │
      not found
        │
        ▼
    llm_generator ──────▶  AgentResponse (source=llm_generated)
        │
        ▼
    → Self-Healing Agent

NEW:
    failed_solutions (List[str]) can be passed to run() so the agent
    explicitly avoids repeating approaches that have already been tried
    and verified as failing.
"""

import re
from typing import List, Optional

import ollama
from qdrant_client import QdrantClient

from agents.knowledge_agent.shared.models import AgentResponse, RAGResult, RAGSource, ErrorCategory
from agents.knowledge_agent.shared.config import Config
from agents.knowledge_agent.knowledge_core.retriever       import retrieve
from agents.knowledge_agent.knowledge_core.research_agent  import generate_solution
from agents.knowledge_agent.knowledge_core.knowledge_graph import KnowledgeGraph


class KnowledgeAgent:

    def __init__(self, config: Config):
        self.config = config
        self.client = QdrantClient(
            host=config.qdrant_host,
            port=config.qdrant_port,
        )
        self.graph = KnowledgeGraph()

    def run(
        self,
        error_message    : str,
        failed_solutions : Optional[List[str]] = None,
    ) -> AgentResponse:
        """
        Investigate an error and return a solution.

        Parameters
        ----------
        error_message    : The full error / incident description.
        failed_solutions : List of healing_prompt summaries from previous
                           attempts that were verified as FAILED.
                           Passed through to the LLM so it knows what NOT
                           to suggest again.
        """
        failed_solutions = failed_solutions or []
        print(f"\n[KnowledgeAgent] Processing: {error_message[:100]}...")
        if failed_solutions:
            print(
                f"[KnowledgeAgent] Excluding {len(failed_solutions)} "
                f"previously-failed solution(s)."
            )

        # ── try knowledge base first ─────────────────────────────────────
        retrieval = retrieve(error_message, self.client, self.config, self.graph)

        if retrieval.found:
            print(f"[KnowledgeAgent] Found in knowledge base (score={retrieval.score})")
            entry = retrieval.entry

            formatted_prompt = self._format_with_llm(
                error_message,
                entry,
                failed_solutions=failed_solutions,
            )

            return AgentResponse(
                source=RAGSource.KNOWLEDGE_BASE,
                confidence=retrieval.score,
                healing_prompt=formatted_prompt,
                suggested_commands=self._extract_commands(formatted_prompt),
                category=ErrorCategory(entry.get("category", "Unknown")),
                action_needed=True,
                rag_result=RAGResult(
                    entry_id=entry.get("id", ""),
                    category=ErrorCategory(entry.get("category", "Unknown")),
                    confidence=retrieval.score,
                    healing_prompt=formatted_prompt,
                    root_cause=entry.get("root_cause", ""),
                    error_pattern=entry.get("error_pattern", ""),
                ),
            )

        # ── fallback: LLM + web search ───────────────────────────────────
        print(
            f"[KnowledgeAgent] Not found (score={retrieval.score}) "
            f"→ calling LLM generator..."
        )
        solution = generate_solution(
            error_message,
            self.config,
            failed_solutions=failed_solutions,
        )

        return AgentResponse(
            source=RAGSource.LLM_GENERATED,
            confidence=solution.confidence,
            healing_prompt=solution.healing_prompt,
            suggested_commands=self._extract_commands(solution.healing_prompt),
            category=ErrorCategory.UNKNOWN,
            action_needed=True,
            web_sources=solution.web_sources,
        )

    def _format_with_llm(
        self,
        error_message    : str,
        entry            : dict,
        failed_solutions : Optional[List[str]] = None,
    ) -> str:
        failed_solutions = failed_solutions or []

        # Build a "do not repeat" block for the LLM
        avoid_block = ""
        if failed_solutions:
            avoid_lines = "\n".join(
                f"  - Attempt {i+1}: {s[:200]}"
                for i, s in enumerate(failed_solutions)
            )
            avoid_block = f"""
⚠️  PREVIOUSLY ATTEMPTED SOLUTIONS (DO NOT REPEAT THESE):
The following approaches have already been tried and verified as FAILED.
Propose a DIFFERENT fix that avoids these approaches entirely:
{avoid_lines}
"""

        prompt = f"""You are a senior DevOps engineer.

The following error occurred:
{error_message}

Here is the known solution from our knowledge base:
Root cause: {entry.get('root_cause', '')}
Healing guide: {entry.get('healing_prompt', '')}
{avoid_block}
Rewrite this as a clear, friendly, step-by-step explanation that:
1. Explains what went wrong in simple terms
2. Gives exact steps to fix it
3. Includes the relevant commands
4. Does NOT repeat any previously-failed approach listed above
"""
        print(f"[KnowledgeAgent] Formatting KB answer with Ollama...")
        response = ollama.chat(
            model=self.config.generation_model,
            messages=[{"role": "user", "content": prompt}]
        )
        return response["message"]["content"]

    @staticmethod
    def _extract_commands(healing_prompt: str) -> list[str]:
        commands = []

        bash_blocks = re.findall(r"```(?:bash|sh)\n(.*?)```", healing_prompt, re.DOTALL)
        for block in bash_blocks:
            for line in block.splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    commands.append(line)

        if not commands:
            for line in healing_prompt.splitlines():
                line = line.strip()
                if line.startswith(("kubectl", "docker", "helm", "git")):
                    commands.append(line)

        return commands