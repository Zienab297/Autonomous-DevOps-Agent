"""
knowledge_core/research_agent.py
---------------------------------
Called when Qdrant score < threshold.
Step 1: Ollama improves the search query
Step 2: Web search using that query
Step 3: Ollama generates solution with references
Returns GeneratedSolution.

CHANGE: generate_solution() now accepts failed_solutions: List[str].
These are injected into the Ollama prompt so the LLM never proposes
an approach that was already tried and verified as FAILED.
"""

from typing import List, Optional

import ollama
from agents.knowledge_agent.shared.models import GeneratedSolution, RAGSource
from agents.knowledge_agent.shared.config import Config
from agents.knowledge_agent.tools.web_search_tool import web_search, format_results_for_prompt


def _generate_search_query(error_message: str, config: Config) -> str:
    """Step 1 — Ollama improves the search query from the raw error."""
    prompt = f"""You are a senior DevOps engineer.
Generate a short, precise web search query to find the fix for this error.

Rules:
- Return ONLY the search query — no explanation, no quotes, no punctuation
- Focus on the TECHNICAL error, not the symptoms
- Use keywords like: fix, solution, error, GitHub Actions, Docker, Kubernetes
- Maximum 10 words

ERROR:
{error_message[:300]}

Search query:"""
    response = ollama.chat(
        model=config.generation_model,
        messages=[{"role": "user", "content": prompt}]
    )
    query = response["message"]["content"].strip().strip('"').strip("'").strip("`")
    query = query.split("\n")[0][:100].strip("`")
    print(f"[LLMGenerator] Generated search query: {query}")
    return query


def generate_solution(
    error_message    : str,
    config           : Config,
    failed_solutions : Optional[List[str]] = None,
) -> GeneratedSolution:
    """
    Generate a solution for the given error using web search + Ollama.

    Parameters
    ----------
    error_message    : Full incident description / error text.
    config           : Knowledge Agent config.
    failed_solutions : List of healing_prompt summaries from previous
                       attempts that were verified as FAILED.
                       Injected into the prompt so Ollama proposes a
                       DIFFERENT approach each time.
    """
    failed_solutions = failed_solutions or []

    # ── step 1: Ollama improves search query ─────────────────────────────
    search_query = _generate_search_query(error_message, config)

    # ── step 2: web search using improved query ───────────────────────────
    print(f"[LLMGenerator] Searching web...")
    try:
        search_results = web_search(query=search_query, max_results=5)
    except Exception as e:
        print(f"[LLMGenerator] Web search failed ({e}) — continuing without results")
        search_results = []
    web_context    = format_results_for_prompt(search_results)

    # ── step 3: build references list ────────────────────────────────────
    references = "\n".join(
        [f"[{i+1}] {r.url}" for i, r in enumerate(search_results)]
    )

    # ── step 4: build "do not repeat" block ──────────────────────────────
    avoid_block = ""
    if failed_solutions:
        avoid_lines = "\n".join(
            f"  - Attempt {i+1}: {s[:200]}"
            for i, s in enumerate(failed_solutions)
        )
        avoid_block = f"""
⚠️  PREVIOUSLY ATTEMPTED SOLUTIONS — DO NOT REPEAT THESE:
The following approaches have already been applied to this incident
and were verified as FAILED by running real commands against the system.
You MUST propose a completely different fix that avoids all of these:

{avoid_lines}

"""

    # ── step 5: build prompt ──────────────────────────────────────────────
    has_results = bool(search_results)

    prompt = f"""You are a senior DevOps engineer analyzing a real production incident.

INCIDENT:
{error_message[:500]}
{avoid_block}
{"WEB SEARCH RESULTS:" if has_results else "NOTE: No web search results found."}
{web_context if has_results else ""}

STRICT RULES — you MUST follow these:
1. Base your answer ONLY on the error message and web results above
2. If web results are irrelevant or empty — say so and base answer on error message only
3. NEVER invent URLs, commands, or solutions you are not confident about
4. If you are not sure about a command — do NOT include it
5. Commands must be real, runnable shell/kubectl/docker commands
6. Confidence must reflect how sure you are based on AVAILABLE evidence
7. Do NOT repeat any approach listed in "PREVIOUSLY ATTEMPTED SOLUTIONS" above

Respond in this EXACT format:

ROOT CAUSE:
<one sentence — what caused this error based on the evidence>

HEALING STEPS:
<numbered list — only steps you are confident about>

COMMANDS:
<only real, runnable commands — leave empty if unsure>

REFERENCES:
{references if has_results else "No references — solution based on error analysis only"}

CONFIDENCE:
<0.0 to 1.0 — be honest, lower is better than hallucinating>"""

    # ── step 6: call Ollama ───────────────────────────────────────────────
    print(f"[LLMGenerator] Calling Ollama {config.generation_model}...")
    response      = ollama.chat(
        model=config.generation_model,
        messages=[{"role": "user", "content": prompt}]
    )
    response_text = response["message"]["content"]

    # ── step 7: extract confidence ────────────────────────────────────────
    confidence = 0.70
    for line in response_text.splitlines():
        if line.strip().startswith("CONFIDENCE:"):
            try:
                confidence = float(line.split(":")[1].strip())
            except ValueError:
                pass

    # ── step 8: extract commands ──────────────────────────────────────────
    commands    = []
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