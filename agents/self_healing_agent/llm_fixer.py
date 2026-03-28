"""
self_healing/llm_fixer.py
-----------------------------
Uses pluggable LLM provider via get_llm_provider(agent="healing").
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
import json

from typing import List, Dict, Optional
from agents.self_healing_agent.models import LLMFixResponse, FileToFix
from dotenv import load_dotenv

load_dotenv()

_provider = None  # module-level cache


def _get_provider():
    global _provider
    if _provider is None:
        from providers.llm.llm_selector import get_llm_provider
        _provider = get_llm_provider(agent="healing")
    return _provider


def _chat(prompt: str) -> str:
    from providers.llm.llm_selector import is_quota_error, handle_quota_error
    global _provider
    provider = _get_provider()
    try:
        return provider.chat(messages=[{"role": "user", "content": prompt}]).content
    except Exception as e:
        if is_quota_error(e):
            new_p = handle_quota_error(provider, agent="healing")
            if new_p:
                _provider = new_p
                return new_p.chat(messages=[{"role": "user", "content": prompt}]).content
        raise


# ── helpers ───────────────────────────────────────────────────────────────────

def _build_files_block(files_to_modify: List[FileToFix]) -> str:
    if not files_to_modify:
        return "No files provided."

    blocks = []
    for i, f in enumerate(files_to_modify, 1):
        current = f.current_content.strip()
        content_display = current if current else "(file does not exist yet — create it)"

        enrichment_lines = []
        if f.line:
            enrichment_lines.append(
                f"  fix at         : line {f.line}"
                + (f"  in {f.function}()" if f.function else "")
            )
        if f.exception:
            enrichment_lines.append(f"  exception      : {f.exception}")
        if f.fix_description:
            enrichment_lines.append(f"  hint           : {f.fix_description}")

        enrichment_block = "\n".join(enrichment_lines)
        blocks.append(
            f"FILE {i}:\n"
            f"  path           : {f.path}\n"
            + (enrichment_block + "\n" if enrichment_block else "")
            + f"  current content:\n{content_display}"
        )
    return "\n\n".join(blocks)


def _parse_json_block(text: str, key: str):
    marker = f"{key}:"
    if marker not in text:
        return None
    segment = text.split(marker, 1)[1]
    start   = segment.find("```json")
    if start == -1:
        return None
    inner = segment[start + 7:]
    end   = inner.find("```")
    if end == -1:
        return None
    try:
        return json.loads(inner[:end].strip())
    except (json.JSONDecodeError, ValueError):
        return None


def _parse_steps(text: str) -> List[str]:
    steps      = []
    in_section = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("STEPS:"):
            in_section = True
            continue
        if in_section:
            if any(stripped.startswith(h) for h in ("MODIFIED_FILES:", "CONFIDENCE:", "REMEDIATION_COMMANDS:")):
                break
            if stripped and stripped[0].isdigit():
                step = stripped.lstrip("0123456789").lstrip(".) ").strip()
                if step:
                    steps.append(step)
    return steps


def _parse_confidence(text: str) -> float:
    lines = text.splitlines()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("CONFIDENCE:"):
            inline = stripped.split(":", 1)[1].strip()
            if inline:
                try:
                    return max(0.0, min(1.0, float(inline)))
                except ValueError:
                    pass
            for j in range(i + 1, len(lines)):
                next_val = lines[j].strip()
                if next_val:
                    try:
                        return max(0.0, min(1.0, float(next_val)))
                    except ValueError:
                        break
    return 0.70


def _parse_remediation_commands(text: str) -> List[Dict]:
    commands = _parse_json_block(text, "REMEDIATION_COMMANDS")
    if not isinstance(commands, list):
        return []
    normalised = []
    for i, cmd in enumerate(commands):
        if not isinstance(cmd, dict) or not cmd.get("command"):
            continue
        normalised.append({
            "command"    : cmd.get("command", "").strip(),
            "description": cmd.get("description", ""),
            "order"      : int(cmd.get("order", i + 1)),
            "on_failure" : cmd.get("on_failure", "abort"),
        })
    normalised.sort(key=lambda c: c["order"])
    return normalised


# ── main entry ────────────────────────────────────────────────────────────────

def fix_files(
    incident_id        : str,
    root_cause         : str,
    healing_prompt     : str,
    suggested_commands : List[str],
    files_to_modify    : List[FileToFix],
) -> LLMFixResponse:

    files_block    = _build_files_block(files_to_modify)
    commands_block = "\n".join(suggested_commands) if suggested_commands else "None"

    import platform
    os_name = "Windows (cmd.exe / PowerShell)" if platform.system() == "Windows" else f"Linux ({platform.system()})"

    prompt = f"""You are a senior DevOps engineer operating as an autonomous self-healing agent.

━━━━━━━━━━━━━━━━━━  ENVIRONMENT  ━━━━━━━━━━━━━━━━━━
OPERATING SYSTEM : {os_name}

━━━━━━━━━━━━━━━━━━  INCIDENT CONTEXT  ━━━━━━━━━━━━━━━━━━
INCIDENT ID   : {incident_id}

ROOT CAUSE:
{root_cause}

HEALING PLAN:
{healing_prompt}

SUGGESTED COMMANDS:
{commands_block}

━━━━━━━━━━━━━━━━━━  FILES TO MODIFY  ━━━━━━━━━━━━━━━━━━
{files_block}

━━━━━━━━━━━━━━━━━━  YOUR TASK  ━━━━━━━━━━━━━━━━━━
For EVERY file: choose action (overwrite/append/replace_line) and produce complete new content.

STRICT RULES:
① NEVER use `pip install -r <file>` — install directly: `pip install pkg==ver`
② NEVER use `systemctl`, `service` — not available on all platforms
③ Health checks must use `on_failure="continue"`
④ Every command must be valid on: {os_name}

Respond in this EXACT format:

STEPS:
1. <what you changed and why>

MODIFIED_FILES:
```json
[
  {{
    "path": "<path>",
    "action": "<overwrite | append | replace_line>",
    "new_content": "<full file text with \\n for newlines>"
  }}
]
```

REMEDIATION_COMMANDS:
```json
[
  {{
    "command": "<exact command>",
    "description": "<one-line explanation>",
    "order": <integer>,
    "on_failure": "<abort | continue | retry>"
  }}
]
```

CONFIDENCE:
<float 0.0–1.0>
"""

    provider_name = _get_provider().name if _get_provider() else "unknown"
    print(f"[LLMFixer] Calling {provider_name} for incident {incident_id}...")

    raw = _chat(prompt)

    modified_files       = _parse_json_block(raw, "MODIFIED_FILES") or []
    steps                = _parse_steps(raw)
    remediation_commands = _parse_remediation_commands(raw)
    confidence           = _parse_confidence(raw)

    print(
        f"[LLMFixer] Done — "
        f"files={len(modified_files)}, steps={len(steps)}, "
        f"cmds={len(remediation_commands)}, confidence={confidence}"
    )

    return LLMFixResponse(
        incident_id          = incident_id,
        modified_files       = modified_files,
        steps                = steps,
        remediation_commands = remediation_commands,
        confidence           = confidence,
        raw_response         = raw,
    )