"""
self_healing/llm_verifier.py
------------------------------
Verification step — runs AFTER the LLM Fixer has applied file changes.

Workflow:
    Step 1 — LLM generates verification commands based on the problem
             and the before/after file diff
    Step 2 — Each command is executed via subprocess
    Step 3 — LLM analyzes all results (exit_code, stdout, stderr)
             and produces a VerificationReport
    Step 4 — Return VerificationReport to the Self-Healing Agent
"""

import json
import logging
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import List, Optional
from groq import Groq
from agents.self_healing_agent.models import VerificationStatus, CommandResult, VerificationReport
import os
from dotenv import load_dotenv

load_dotenv()
client = Groq(api_key=os.environ.get("GROQ_API_KEY"))

logger = logging.getLogger(__name__)

MODEL = "openai/gpt-oss-120b"


# ============================================================
# Helpers
# ============================================================

def _build_diff_block(modifications: list) -> str:
    """
    Render a before/after diff block for each modified file
    so the LLM understands exactly what changed.
    """
    if not modifications:
        return "No file modifications provided."

    blocks = []
    for mod in modifications:
        blocks.append(
            f"FILE : {mod.path}\n"
            f"ACTION: {mod.action}\n"
            f"\n── BEFORE ──\n{mod.old_content.strip()}\n"
            f"\n── AFTER  ──\n{mod.new_content.strip()}"
        )
    return "\n\n" + "─" * 50 + "\n\n".join(blocks)


def _parse_commands_block(text: str) -> List[str]:
    """
    Extract the JSON array of commands from VERIFICATION_COMMANDS section.
    Falls back to line-by-line parsing if JSON is malformed.
    """
    marker = "VERIFICATION_COMMANDS:"
    if marker not in text:
        return []

    segment = text.split(marker, 1)[1]

    # Try fenced JSON first
    start = segment.find("```json")
    if start != -1:
        inner = segment[start + 7:]
        end   = inner.find("```")
        if end != -1:
            try:
                parsed = json.loads(inner[:end].strip())
                if isinstance(parsed, list):
                    return [str(c).strip() for c in parsed if str(c).strip()]
            except (json.JSONDecodeError, ValueError):
                pass

    # Fallback: grab non-empty lines until next section
    commands = []
    for line in segment.splitlines():
        stripped = line.strip()
        if stripped.startswith(("ANALYSIS:", "STATUS:", "REASON:", "CONFIDENCE:")):
            break
        # strip list markers: "1.", "-", "*"
        cleaned = stripped.lstrip("0123456789.-*) ").strip()
        if cleaned and not cleaned.startswith("#"):
            commands.append(cleaned)
    return commands


def _parse_field(text: str, key: str) -> str:
    """Extract value after a section header — supports multiline."""
    marker = f"{key}:"
    lines  = text.splitlines()

    for i, line in enumerate(lines):
        if line.strip().startswith(marker):
            # grab the rest of this line
            inline = line.split(":", 1)[1].strip()
            if inline:
                return inline

            # if nothing on same line, grab next non-empty line
            for j in range(i + 1, len(lines)):
                next_line = lines[j].strip()
                if next_line and not next_line.endswith(":"):
                    return next_line
    return ""


def _parse_confidence(text: str) -> float:
    raw = _parse_field(text, "CONFIDENCE")
    try:
        return max(0.0, min(1.0, float(raw)))
    except ValueError:
        return 0.5


def _parse_status(text: str) -> VerificationStatus:
    raw = _parse_field(text, "STATUS").upper()
    if "PASS" in raw:
        return VerificationStatus.PASS
    if "FAIL" in raw:
        return VerificationStatus.FAIL
    return VerificationStatus.UNKNOWN


# ============================================================
# Step 1 — Generate verification commands via LLM
# ============================================================

def _generate_verification_commands(incident_id: str, root_cause: str,
                                    healing_prompt: str, diff_block: str,
                                    file_paths: List[str]) -> tuple[List[str], str]:
    """
    Ask the LLM which commands should be run to verify the fix worked.

    Returns (commands, raw_response)
    """
    import platform
    os_name = "Windows (cmd.exe / PowerShell)" if platform.system() == "Windows" else f"Linux ({platform.system()})"

    paths_block = "\n".join(f"  • {p}" for p in file_paths) if file_paths else "  (none)"

    prompt = f"""You are a senior DevOps engineer performing post-fix verification.
A self-healing agent has just modified files to resolve an incident.
Your job is to generate shell commands that VERIFY the fix actually worked.

━━━━━━━━━━━━━━━━━━  ENVIRONMENT  ━━━━━━━━━━━━━━━━━━
OPERATING SYSTEM : {os_name}
All commands MUST work on this OS. Do not use commands from other platforms.

━━━━━━━━━━━━━━━━━━  INCIDENT  ━━━━━━━━━━━━━━━━━━
INCIDENT ID : {incident_id}

ROOT CAUSE:
{root_cause}

HEALING APPLIED:
{healing_prompt}

━━━━━━━━━━━━━━━━━━  MODIFIED FILE PATHS  ━━━━━━━━━━━━━━━━━━
These are the EXACT absolute paths of every file that was changed.
Always reference these paths directly in your commands — never guess a relative path.
{paths_block}

━━━━━━━━━━━━━━━━━━  FILE CHANGES  ━━━━━━━━━━━━━━━━━━
{diff_block}

━━━━━━━━━━━━━━━━━━  YOUR TASK  ━━━━━━━━━━━━━━━━━━
Generate 2–4 commands that verify the fix is correct.

RULES:
  ✓ Commands must work on {os_name} — no grep, curl, cat, ls on Windows
  ✓ For Python package checks: use `pip show <pkg>` or `python -c "import pkg"`
  ✓ For file content checks: use python -c "print(open(r'ABSOLUTE_PATH').read())"
  ✓ Always use the EXACT absolute file paths listed above — never relative paths
  ✗ Do NOT use Linux-only commands on Windows (grep, cat, ls, curl, etc.)
  ✗ Do NOT modify any files
  ✗ Do NOT use docker-compose up / kubectl apply

Respond in this EXACT format:

VERIFICATION_COMMANDS:
```json
["command1", "command2"]
```
"""

    logger.info(f"[LLMVerifier] Generating verification commands...")
    response = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = response.choices[0].message.content
    commands = _parse_commands_block(raw)
    logger.info(f"[LLMVerifier] Generated {len(commands)} commands")
    return commands, raw


# ============================================================
# Step 2 — Execute commands
# ============================================================

def _run_commands(commands: List[str], timeout: int = 30) -> List[CommandResult]:
    """
    Run each command via subprocess and collect results.

    Parameters
    ----------
    commands : list of shell command strings
    timeout  : seconds before a command is killed (default 30)
    """
    results = []

    for cmd in commands:
        logger.info(f"[LLMVerifier] Running: {cmd}")
        try:
            proc = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            result = CommandResult(
                command=cmd,
                exit_code=proc.returncode,
                stdout=proc.stdout.strip(),
                stderr=proc.stderr.strip(),
                passed=proc.returncode == 0,
            )
        except subprocess.TimeoutExpired:
            result = CommandResult(
                command=cmd,
                exit_code=-1,
                stdout="",
                stderr="",
                passed=False,
                error=f"Command timed out after {timeout}s",
            )
        except Exception as e:
            result = CommandResult(
                command=cmd,
                exit_code=-1,
                stdout="",
                stderr="",
                passed=False,
                error=str(e),
            )

        logger.info(f"[LLMVerifier] {result}")
        results.append(result)

    return results


# ============================================================
# Step 3 — LLM analyzes results
# ============================================================

def _analyze_results( incident_id: str, root_cause: str, results: List[CommandResult],) -> tuple[VerificationStatus, str, float, str]:
    """
    Feed command outputs back to the LLM for final verdict.

    Returns (status, reason, confidence, raw_response)
    """
    # Build results block
    results_block = ""
    for i, r in enumerate(results, 1):
        results_block += (
            f"\nCOMMAND {i}: {r.command}\n"
            f"  exit_code : {r.exit_code}\n"
            f"  stdout    : {r.stdout[:300] or '(empty)'}\n"
            f"  stderr    : {r.stderr[:300] or '(empty)'}\n"
            f"  passed    : {r.passed}\n"
            + (f"  error     : {r.error}\n" if r.error else "")
        )

    prompt = f"""You are a senior DevOps engineer analyzing the results of
post-fix verification commands.

━━━━━━━━━━━━━━━━━━  INCIDENT  ━━━━━━━━━━━━━━━━━━
INCIDENT ID : {incident_id}

ROOT CAUSE:
{root_cause}

━━━━━━━━━━━━━━━━━━  VERIFICATION RESULTS  ━━━━━━━━━━━━━━━━━━
{results_block}

━━━━━━━━━━━━━━━━━━  YOUR TASK  ━━━━━━━━━━━━━━━━━━
Analyze the command results above and determine if the fix was successful.

Respond in this EXACT format:

STATUS:
<PASS | FAIL | UNKNOWN>

REASON:
<one clear sentence explaining your verdict based on the actual outputs>

CONFIDENCE:
<float 0.0–1.0>
"""

    logger.info(f"[LLMVerifier] Analyzing results...")
    response = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}]
    )
    raw = response.choices[0].message.content
    status     = _parse_status(raw)
    reason     = _parse_field(raw, "REASON")
    confidence = _parse_confidence(raw)

    return status, reason, confidence, raw


# ============================================================
# Main entry
# ============================================================

def verify_fix(
    incident_id:    str,
    root_cause:     str,
    healing_prompt: str,
    modifications:  list,           # List[FileModificationResult]
) -> VerificationReport:
    """
    Full verification pipeline.

    Parameters
    ----------
    incident_id    : forwarded from Solution / SelfHealingResult
    root_cause     : one-line diagnosis
    healing_prompt : narrative of what was fixed
    modifications  : List[FileModificationResult] from SelfHealingResult

    Returns
    -------
    VerificationReport
    """
    # ── step 1: build diff block + generate commands ──────────────────
    diff_block = _build_diff_block(modifications)
    file_paths = [m.path for m in modifications if m.path]
    commands, _ = _generate_verification_commands(
        incident_id, root_cause, healing_prompt, diff_block, file_paths
    )

    if not commands:
        logger.warning("[LLMVerifier] No commands generated — returning UNKNOWN")
        return VerificationReport(
            incident_id=incident_id,
            status=VerificationStatus.UNKNOWN,
            commands_run=[],
            results=[],
            reason="LLM did not generate any verification commands.",
            confidence=0.0,
        )

    # ── step 2: run commands ──────────────────────────────────────────
    results = _run_commands(commands)

    # ── step 3: LLM analyzes outputs ─────────────────────────────────
    status, reason, confidence, raw = _analyze_results(
        incident_id, root_cause, results
    )

    logger.info(
        f"[LLMVerifier] Done — "
        f"status={status.value}, confidence={confidence:.2f}"
    )

    return VerificationReport(
        incident_id=incident_id,
        status=status,
        commands_run=commands,
        results=results,
        reason=reason,
        confidence=confidence,
        raw_response=raw,
    )