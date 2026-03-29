"""
knowledge_core/knowledge_agent.py
----------------------------------
Uses the pluggable LLM provider system instead of hardcoded ollama.
Provider is selected once per session via get_llm_provider(agent="knowledge").
"""

import re
from typing import List, Optional

from qdrant_client import QdrantClient

from agents.knowledge_agent.shared.models import AgentResponse, RAGResult, RAGSource, ErrorCategory
from agents.knowledge_agent.shared.config import Config
from agents.knowledge_agent.knowledge_core.retriever       import retrieve
from agents.knowledge_agent.knowledge_core.research_agent  import generate_solution
from agents.knowledge_agent.knowledge_core.knowledge_graph import KnowledgeGraph


class KnowledgeAgent:

    def __init__(self, config: Config):
        self.config   = config
        self.client   = QdrantClient(
            host=config.qdrant_host,
            port=config.qdrant_port,
        )
        self.graph    = KnowledgeGraph()
        self._provider = None   # lazy — set on first use

    def _get_provider(self):
        """Lazy-load provider — asks user once, reuses after that."""
        if self._provider is None:
            from providers.llm.llm_selector import get_llm_provider
            self._provider = get_llm_provider(agent="knowledge")
        return self._provider

    def run(
        self,
        error_message    : str,
        failed_solutions : Optional[List[str]] = None,
    ) -> AgentResponse:
        failed_solutions = failed_solutions or []
        print(f"\n[KnowledgeAgent] Processing: {error_message[:100]}...")
        if failed_solutions:
            print(f"[KnowledgeAgent] Excluding {len(failed_solutions)} previously-failed solution(s).")

        retrieval = retrieve(error_message, self.client, self.config, self.graph)

        if retrieval.found:
            print(f"[KnowledgeAgent] Found in knowledge base (score={retrieval.score})")
            entry            = retrieval.entry
            formatted_prompt = self._format_with_llm(error_message, entry, failed_solutions)

            return AgentResponse(
                source             = RAGSource.KNOWLEDGE_BASE,
                confidence         = retrieval.score,
                healing_prompt     = formatted_prompt,
                suggested_commands = self._extract_commands(formatted_prompt),
                category           = ErrorCategory(entry.get("category", "Unknown")),
                action_needed      = True,
                rag_result         = RAGResult(
                    entry_id      = entry.get("id", ""),
                    category      = ErrorCategory(entry.get("category", "Unknown")),
                    confidence    = retrieval.score,
                    healing_prompt= formatted_prompt,
                    root_cause    = entry.get("root_cause", ""),
                    error_pattern = entry.get("error_pattern", ""),
                ),
            )

        print(f"[KnowledgeAgent] Not found (score={retrieval.score}) → calling LLM generator...")
        solution = generate_solution(error_message, self.config, failed_solutions=failed_solutions)

        return AgentResponse(
            source             = RAGSource.LLM_GENERATED,
            confidence         = solution.confidence,
            healing_prompt     = solution.healing_prompt,
            suggested_commands = self._extract_commands(solution.healing_prompt),
            category           = ErrorCategory.UNKNOWN,
            action_needed      = True,
            web_sources        = solution.web_sources,
        )

    def _format_with_llm(
        self,
        error_message    : str,
        entry            : dict,
        failed_solutions : Optional[List[str]] = None,
    ) -> str:
        failed_solutions = failed_solutions or []

        avoid_block = ""
        if failed_solutions:
            avoid_lines = "\n".join(
                f"  - Attempt {i+1}: {s[:200]}"
                for i, s in enumerate(failed_solutions)
            )
            avoid_block = f"""
⚠️  PREVIOUSLY ATTEMPTED SOLUTIONS (DO NOT REPEAT THESE):
{avoid_lines}
"""

        prompt = f"""You are a senior DevOps engineer.

The following error occurred:
{error_message}

Known solution from knowledge base:
Root cause: {entry.get('root_cause', '')}
Healing guide: {entry.get('healing_prompt', '')}
{avoid_block}
Rewrite as clear step-by-step instructions:
1. Explain what went wrong simply
2. Give exact steps to fix it
3. Include relevant commands
4. Do NOT repeat any previously-failed approach
"""
        print(f"[KnowledgeAgent] Formatting KB answer with LLM...")

        provider = self._get_provider()
        try:
            response = provider.chat(
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content
        except Exception as e:
            from providers.llm.llm_selector import is_quota_error, handle_quota_error
            if is_quota_error(e):
                new_provider = handle_quota_error(provider, agent="knowledge")
                if new_provider:
                    self._provider = new_provider
                    response = new_provider.chat(messages=[{"role": "user", "content": prompt}])
                    return response.content
            raise

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