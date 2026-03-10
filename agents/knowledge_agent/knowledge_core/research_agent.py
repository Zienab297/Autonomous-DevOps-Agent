"""
agent/llm_generator.py
-----------------------
Called when Qdrant score < threshold.
Uses web_search_tool to get context → builds prompt → calls Gemini.
Returns GeneratedSolution.
"""

from google import genai

from core.models import GeneratedSolution, RAGSource
from core.config import Config
from tools.web_search_tool import web_search, format_results_for_prompt


def generate_solution(error_message: str, config: Config) -> GeneratedSolution:
    client = genai.Client(api_key=config.gemini_api_key)

    # ── step 1: web search ───────────────────────────────────────────────
    print(f"[LLMGenerator] Searching web for: {error_message[:80]}...")
    search_results = web_search(
        query=f"fix devops error: {error_message}",
        max_results=5,
    )
    web_context = format_results_for_prompt(search_results)

    # ── step 2: build prompt ─────────────────────────────────────────────
    prompt = f"""You are a senior DevOps engineer and AI healing agent.

A system has encountered the following error:
{error_message}

Here are relevant web search results that may help:
{web_context}

Your task:
1. Identify the root cause of this error
2. Provide a step-by-step healing prompt that an automated agent can follow
3. Include specific commands to fix the issue
4. Be concrete and actionable

Respond in this exact format:

ROOT CAUSE:
<one sentence explaining why this error occurs>

HEALING STEPS:
<numbered list of steps to fix>

COMMANDS:
<shell/kubectl/docker commands, one per line>

CONFIDENCE:
<a number between 0.0 and 1.0 indicating how confident you are>
"""

    # ── step 3: call Gemini ──────────────────────────────────────────────
    print(f"[LLMGenerator] Calling Gemini {config.generation_model}...")
    response = client.models.generate_content(
        model=config.generation_model,
        contents=prompt,
    )
    response_text = response.text

    # ── step 4: extract confidence ───────────────────────────────────────
    confidence = 0.70
    for line in response_text.splitlines():
        if line.strip().startswith("CONFIDENCE:"):
            try:
                confidence = float(line.split(":")[1].strip())
            except ValueError:
                pass

    # ── step 5: extract commands ─────────────────────────────────────────
    commands = []
    in_commands = False
    for line in response_text.splitlines():
        if line.strip().startswith("COMMANDS:"):
            in_commands = True
            continue
        if in_commands:
            if line.strip().startswith("CONFIDENCE:"):
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