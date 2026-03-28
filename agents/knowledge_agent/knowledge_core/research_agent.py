"""
knowledge_core/research_agent.py
---------------------------------
Web search + LLM fallback when Qdrant score < threshold.
Uses pluggable LLM provider via get_llm_provider(agent="knowledge").
"""

from typing import List, Optional

from agents.knowledge_agent.shared.models import GeneratedSolution, RAGSource
from agents.knowledge_agent.shared.config import Config
from agents.knowledge_agent.tools.web_search_tool import web_search, format_results_for_prompt

_provider = None   # module-level cache — reset on quota error


def _get_provider():
    global _provider
    if _provider is None:
        from providers.llm.llm_selector import get_llm_provider
        _provider = get_llm_provider(agent="knowledge")
    return _provider


def set_provider(p):
    """Allow external code (e.g. knowledge_agent.py) to share the same provider."""
    global _provider
    _provider = p


def _chat(prompt: str) -> str:
    from providers.llm.llm_selector import is_quota_error, handle_quota_error
    global _provider
    provider = _get_provider()
    try:
        return provider.chat(messages=[{"role": "user", "content": prompt}]).content
    except Exception as e:
        if is_quota_error(e):
            new_p = handle_quota_error(provider, agent="knowledge")
            if new_p:
                _provider = new_p   # ← update cache with new provider
                return new_p.chat(messages=[{"role": "user", "content": prompt}]).content
        raise


def _generate_search_query(error_message: str, config: Config) -> str:
    prompt = f"""You are a senior DevOps engineer.
Given this error, generate a short, precise web search query to find the fix.
Return ONLY the search query, nothing else.

ERROR:
{error_message}
"""
    query = _chat(prompt).strip()
    print(f"[LLMGenerator] Generated search query: {query}")
    return query


def generate_solution(
    error_message    : str,
    config           : Config,
    failed_solutions : Optional[List[str]] = None,
) -> GeneratedSolution:
    failed_solutions = failed_solutions or []

    search_query  = _generate_search_query(error_message, config)
    print(f"[LLMGenerator] Searching web...")
    search_results = web_search(query=search_query, max_results=5)
    web_context    = format_results_for_prompt(search_results)
    references     = "\n".join(f"[{i+1}] {r.url}" for i, r in enumerate(search_results))

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

    prompt = f"""You are a senior DevOps engineer and AI healing agent.

A system has encountered the following error:
{error_message}

Relevant web search results:
{web_context}
{avoid_block}
Your task:
1. Identify the root cause
2. Provide step-by-step healing instructions an automated agent can follow
3. Include specific commands
4. Do NOT repeat any previously-failed approach listed above

Respond in this exact format:

ROOT CAUSE:
<one sentence>

HEALING STEPS:
<numbered list>

COMMANDS:
<shell/kubectl/docker commands, one per line>

REFERENCES:
{references}

CONFIDENCE:
<0.0-1.0>
"""

    print(f"[LLMGenerator] Calling LLM...")
    response_text = _chat(prompt)

    confidence = 0.70
    for line in response_text.splitlines():
        if line.strip().startswith("CONFIDENCE:"):
            try:
                confidence = float(line.split(":")[1].strip())
            except ValueError:
                pass

    commands = []
    in_commands = False
    for line in response_text.splitlines():
        if line.strip().startswith("COMMANDS:"):
            in_commands = True
            continue
        if in_commands:
            if line.strip().startswith(("REFERENCES:", "CONFIDENCE:")):
                break
            if line.strip():
                commands.append(line.strip())

    print(f"[LLMGenerator] Done — confidence={confidence}")

    return GeneratedSolution(
        healing_prompt     = response_text,
        confidence         = confidence,
        source             = RAGSource.LLM_GENERATED,
        web_sources        = [r.url for r in search_results],
        suggested_commands = commands,
    )