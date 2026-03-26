from setup_path import *
"""
knowledge_core/research_agent.py
---------------------------------
Called when Qdrant score < threshold.
Step 1: Ollama improves the search query
Step 2: Web search using that query
Step 3: Ollama generates solution with references
Returns GeneratedSolution.
"""

import ollama
from agents.knowledge_agent.shared.models import GeneratedSolution, RAGSource
from agents.knowledge_agent.shared.config import Config
from agents.knowledge_agent.tools.web_search_tool import web_search, format_results_for_prompt


def _generate_search_query(error_message: str, config: Config) -> str:
    """Step 1 — Ollama improves the search query from the raw error."""
    prompt = f"""You are a senior DevOps engineer.
Given this error, generate a short, precise web search query to find the fix.
Return ONLY the search query, nothing else.

ERROR:
{error_message}
"""
    response = ollama.chat(
        model=config.generation_model,
        messages=[{"role": "user", "content": prompt}]
    )
    query = response["message"]["content"].strip()
    print(f"[LLMGenerator] Generated search query: {query}")
    return query


def generate_solution(error_message: str, config: Config) -> GeneratedSolution:

    # ── step 1: Ollama improves search query ─────────────────────────────
    search_query = _generate_search_query(error_message, config)

    # ── step 2: web search using improved query ───────────────────────────
    print(f"[LLMGenerator] Searching web...")
    search_results = web_search(query=search_query, max_results=5)
    web_context = format_results_for_prompt(search_results)

    # ── step 3: build references list ────────────────────────────────────
    references = "\n".join(
        [f"[{i+1}] {r.url}" for i, r in enumerate(search_results)]
    )

    # ── step 4: build prompt ──────────────────────────────────────────────
    prompt = f"""You are a senior DevOps engineer and AI healing agent.

A system has encountered the following error:
{error_message}

Here are relevant web search results:
{web_context}

Your task:
1. Identify the root cause of this error
2. Provide a step-by-step healing prompt that an automated agent can follow
3. Include specific commands to fix the issue
4. Mention which source(s) helped you find the solution

Respond in this exact format:

ROOT CAUSE:
<one sentence explaining why this error occurs>

HEALING STEPS:
<numbered list of steps to fix>

COMMANDS:
<shell/kubectl/docker commands, one per line>

REFERENCES:
{references}

CONFIDENCE:
<a number between 0.0 and 1.0 indicating how confident you are>
"""

    # ── step 5: call Ollama ───────────────────────────────────────────────
    print(f"[LLMGenerator] Calling Ollama {config.generation_model}...")
    response = ollama.chat(
        model=config.generation_model,
        messages=[{"role": "user", "content": prompt}]
    )
    response_text = response["message"]["content"]

    # ── step 6: extract confidence ────────────────────────────────────────
    confidence = 0.70
    for line in response_text.splitlines():
        if line.strip().startswith("CONFIDENCE:"):
            try:
                confidence = float(line.split(":")[1].strip())
            except ValueError:
                pass

    # ── step 7: extract commands ──────────────────────────────────────────
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
        healing_prompt=response_text,
        confidence=confidence,
        source=RAGSource.LLM_GENERATED,
        web_sources=[r.url for r in search_results],
        suggested_commands=commands,
    )