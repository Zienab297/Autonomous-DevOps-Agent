"""
self_healing/llm_fixer.py
-----------------------------
Called by the Self-Healing Agent after a Solution is produced.
Receives root_cause, healing_prompt, suggested_commands, and files_to_modify.

Step 1: Build a structured prompt for Ollama (senior DevOps persona)
Step 2: Call Ollama to generate new file contents + remediation steps + remediation commands
Step 3: Parse the response and return LLMFixResponse
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
import json

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Dict, Optional

from models import LLMFixResponse
from groq import Groq
from dotenv import load_dotenv
import os

load_dotenv()
client = Groq(api_key=os.environ.get("GROQ_API_KEY"))  # or set GROQ_API_KEY env variable
# ── constant ──────────────────────────────────────────────────────────────────
MODEL = "openai/gpt-oss-120b"


# ── helpers ──────────────────────────────────────────────────────────────────

def _build_files_block(files_to_modify: List[Dict]) -> str:
    """Render each file entry into a readable block for the prompt."""
    if not files_to_modify:
        return "No files provided."

    blocks = []
    for i, f in enumerate(files_to_modify, 1):
        blocks.append(
            f"FILE {i}:\n"
            f"  path   : {f.get('path', 'unknown')}\n"
            f"  action : {f.get('action', 'unknown')}\n"
            f"  content:\n{f.get('content', '').strip()}"
        )
    return "\n\n".join(blocks)


def _parse_json_block(text: str, key: str) -> any:
    """
    Extract a fenced JSON block that follows a section header.
    e.g.  MODIFIED_FILES:\n```json\n[...]\n```
    """
    marker = f"{key}:"
    if marker not in text:
        return None

    segment = text.split(marker, 1)[1]
    start = segment.find("```json")
    if start == -1:
        return None

    inner = segment[start + 7:]
    end = inner.find("```")
    if end == -1:
        return None

    try:
        return json.loads(inner[:end].strip())
    except (json.JSONDecodeError, ValueError):
        return None


def _parse_steps(text: str) -> List[str]:
    """Extract numbered steps from the STEPS section."""
    steps = []
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
                # strip leading "1. " / "1) "
                step = stripped.lstrip("0123456789").lstrip(".) ").strip()
                if step:
                    steps.append(step)

    return steps


def _parse_confidence(text: str) -> float:
    """
    Extract the confidence float from the CONFIDENCE section.
    Handles both same-line and next-line formats:
        CONFIDENCE: 0.85
        CONFIDENCE:
        0.85
    """
    lines = text.splitlines()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("CONFIDENCE:"):
            # try same line first: "CONFIDENCE: 0.85"
            inline = stripped.split(":", 1)[1].strip()
            if inline:
                try:
                    return max(0.0, min(1.0, float(inline)))
                except ValueError:
                    pass
            # try next non-empty line: "CONFIDENCE:\n0.85"
            for j in range(i + 1, len(lines)):
                next_val = lines[j].strip()
                if next_val:
                    try:
                        return max(0.0, min(1.0, float(next_val)))
                    except ValueError:
                        break
    return 0.70


def _parse_remediation_commands(text: str) -> List[Dict]:
    """
    Extract the REMEDIATION_COMMANDS JSON array from the LLM response.

    Expected format in the response:

        REMEDIATION_COMMANDS:
        ```json
        [
          {
            "command": "systemctl restart nginx",
            "description": "Reload nginx after config update",
            "order": 1,
            "on_failure": "abort"   // "abort" | "continue" | "retry"
          }
        ]
        ```

    Returns a list of command dicts, sorted by "order" if present.
    Falls back to an empty list if the section is missing or unparseable.
    """
    commands = _parse_json_block(text, "REMEDIATION_COMMANDS")
    if not isinstance(commands, list):
        return []

    # Normalise: ensure every entry has the expected keys with safe defaults
    normalised = []
    for i, cmd in enumerate(commands):
        if not isinstance(cmd, dict) or not cmd.get("command"):
            continue
        normalised.append({
            "command":     cmd.get("command", "").strip(),
            "description": cmd.get("description", ""),
            "order":       int(cmd.get("order", i + 1)),
            "on_failure":  cmd.get("on_failure", "abort"),
        })

    # Return in execution order
    normalised.sort(key=lambda c: c["order"])
    return normalised


# ── main entry ────────────────────────────────────────────────────────────────

def fix_files(incident_id: str, root_cause: str, healing_prompt: str,
              suggested_commands: List[str], files_to_modify: List[Dict]) -> LLMFixResponse:
    """
    Core fixer function.

    Parameters
    ----------
    incident_id        : identifier forwarded from Solution
    root_cause         : one-line diagnosis from the Knowledge Agent
    healing_prompt     : full narrative produced by the Knowledge Agent
    suggested_commands : shell/kubectl/docker commands already identified
    files_to_modify    : list of {"path", "action", "content"} dicts

    Returns
    -------
    LLMFixResponse with modified_files, steps, remediation_commands, and confidence
    """

    # ── step 1: render file block ─────────────────────────────────────────
    files_block    = _build_files_block(files_to_modify)
    commands_block = "\n".join(suggested_commands) if suggested_commands else "None"

    import platform
    os_name = "Windows (cmd.exe / PowerShell)" if platform.system() == "Windows" else f"Linux ({platform.system()})"

    # ── step 2: build prompt ──────────────────────────────────────────────
    prompt = f"""You are a senior DevOps engineer operating as an autonomous self-healing agent.
You have already diagnosed an incident. Your job now is to produce the exact new content
for every file that must be changed to resolve the issue, AND the exact shell commands
that must be executed AFTER the files are written to fully remediate the incident.

━━━━━━━━━━━━━━━━━━  ENVIRONMENT  ━━━━━━━━━━━━━━━━━━
OPERATING SYSTEM : {os_name}
All commands MUST be valid on this OS. Do not use commands from other platforms.

━━━━━━━━━━━━━━━━━━  INCIDENT CONTEXT  ━━━━━━━━━━━━━━━━━━
INCIDENT ID   : {incident_id}

ROOT CAUSE:
{root_cause}

HEALING PLAN:
{healing_prompt}

SUGGESTED COMMANDS (hints — refine or extend as needed):
{commands_block}

━━━━━━━━━━━━━━━━━━  FILES TO MODIFY  ━━━━━━━━━━━━━━━━━━
{files_block}

━━━━━━━━━━━━━━━━━━  YOUR TASK  ━━━━━━━━━━━━━━━━━━
For EVERY file listed above:
  • Apply the required action (replace_line / append / overwrite).
  • Produce the COMPLETE new file content — no placeholders, no ellipsis.
  • Preserve all lines that do not need to change.

For REMEDIATION_COMMANDS:
  • Use the SUGGESTED COMMANDS above as your primary source — they are already correct.
  • You may add extra commands (e.g. health checks) but keep them minimal.
  • Order them by execution sequence using the "order" field (1 = first).
  • Set "on_failure" to one of: "abort" (stop), "continue" (skip and proceed), "retry" (retry once).

  STRICT RULES:
  ① NEVER use `pip install -r <file>` — always install packages directly: `pip install pkg==ver`
  ② NEVER use `systemctl`, `service`, `initctl` — not available on all platforms
  ③ NEVER use Linux-only commands (grep, curl, cat, ls) on Windows — use Python or pip instead
  ④ Health checks must use `on_failure="continue"` — the service may not be running
  ⑤ Every command must be valid on: {os_name}

Respond in this EXACT format (do not add extra sections):

STEPS:
1. <what you changed and why — one file or logical group per step>
2. ...

MODIFIED_FILES:
```json
[
  {{
    "path": "<same path as above>",
    "action": "<same action as above>",
    "new_content": "<full file text with \\n for newlines>"
  }}
]
```

REMEDIATION_COMMANDS:
```json
[
  {{
    "command": "<exact shell/kubectl/docker command to run>",
    "description": "<one-line explanation of what this command does and why>",
    "order": <integer, 1 = first>,
    "on_failure": "<abort | continue | retry>"
  }}
]
```

CONFIDENCE:
<float 0.0–1.0>
"""

    # ── step 3: call model ────────────────────────────────────────────────
    print(f"[LLMFixer] Calling {MODEL} for incident {incident_id}...")
    response = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = response.choices[0].message.content

    # ── step 4: parse response ────────────────────────────────────────────
    modified_files        = _parse_json_block(raw, "MODIFIED_FILES") or []
    steps                 = _parse_steps(raw)
    remediation_commands  = _parse_remediation_commands(raw)
    confidence            = _parse_confidence(raw)

    print(
        f"[LLMFixer] Done — "
        f"files={len(modified_files)}, "
        f"steps={len(steps)}, "
        f"cmds={len(remediation_commands)}, "
        f"confidence={confidence}"
    )

    return LLMFixResponse(
        incident_id=incident_id,
        modified_files=modified_files,
        steps=steps,
        remediation_commands=remediation_commands,
        confidence=confidence,
        raw_response=raw,
    )