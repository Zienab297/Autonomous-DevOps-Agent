from setup_path import *
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
"""

import re
import ollama
from qdrant_client import QdrantClient

from shared.models import AgentResponse, RAGResult, RAGSource, ErrorCategory
from shared.config import Config
from knowledge_core.retriever       import retrieve
from knowledge_core.research_agent  import generate_solution
from knowledge_core.knowledge_graph import KnowledgeGraph   # ← جديد


class KnowledgeAgent:

    def __init__(self, config: Config):
        self.config = config
        self.client = QdrantClient(
            host=config.qdrant_host,
            port=config.qdrant_port,
        )
        self.graph = KnowledgeGraph()                        # ← جديد

    def run(self, error_message: str) -> AgentResponse:
        print(f"\n[KnowledgeAgent] Processing: {error_message[:100]}...")

        # ── try knowledge base first ─────────────────────────────────────
        retrieval = retrieve(error_message, self.client, self.config, self.graph)  # ← جديد

        if retrieval.found:
            print(f"[KnowledgeAgent] Found in knowledge base (score={retrieval.score})")
            entry = retrieval.entry

            formatted_prompt = self._format_with_llm(error_message, entry)

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
        print(f"[KnowledgeAgent] Not found (score={retrieval.score}) "
              f"→ calling LLM generator...")
        solution = generate_solution(error_message, self.config)

        return AgentResponse(
            source=RAGSource.LLM_GENERATED,
            confidence=solution.confidence,
            healing_prompt=solution.healing_prompt,
            suggested_commands=self._extract_commands(solution.healing_prompt),
            category=ErrorCategory.UNKNOWN,
            action_needed=True,
            web_sources=solution.web_sources,
        )

    def _format_with_llm(self, error_message: str, entry: dict) -> str:
        prompt = f"""You are a senior DevOps engineer.

The following error occurred:
{error_message}

Here is the known solution from our knowledge base:
Root cause: {entry.get('root_cause', '')}
Healing guide: {entry.get('healing_prompt', '')}

Rewrite this as a clear, friendly, step-by-step explanation that:
1. Explains what went wrong in simple terms
2. Gives exact steps to fix it
3. Includes the relevant commands
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