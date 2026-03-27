"""
self_healing/llm_instructor.py
--------------------------------
Called by the Self-Healing Agent when a Solution has NO files_to_modify
(e.g. the fix requires assigning GitHub secrets, changing cloud provider
settings, rotating credentials, or any other manual human action).

Instead of attempting automated file edits, this module asks the LLM to
produce a clear, numbered, platform-aware instruction set that the operator
can follow immediately in the CLI.

Returns a plain str — the formatted instruction block.
"""

import os
import platform
from typing import List

from groq import Groq
from dotenv import load_dotenv

load_dotenv()
client = Groq(api_key=os.environ.get("GROQ_API_KEY"))

MODEL = "openai/gpt-oss-120b"


def generate_instructions(
    incident_id        : str,
    root_cause         : str,
    healing_prompt     : str,
    suggested_commands : List[str],
) -> str:
    """
    Ask the LLM to produce step-by-step human instructions for an incident
    that cannot be auto-remediated (no code files to modify).

    Parameters
    ----------
    incident_id        : The incident identifier (for display only).
    root_cause         : One-line diagnosis from the Knowledge Agent.
    healing_prompt     : Full narrative solution from the Knowledge Agent.
    suggested_commands : Any shell/CLI commands already identified.

    Returns
    -------
    A formatted string with numbered steps ready to print in the terminal.
    """
    os_name   = (
        "Windows (cmd.exe / PowerShell)"
        if platform.system() == "Windows"
        else f"Linux ({platform.system()})"
    )
    cmds_block = "\n".join(f"  • {c}" for c in suggested_commands) if suggested_commands else "  (none)"

    prompt = f"""You are a senior DevOps engineer helping an operator resolve an incident
that CANNOT be fixed by modifying code files automatically.

The operator is sitting at a terminal and needs clear, actionable instructions
they can follow RIGHT NOW.

━━━━━━━━━━━━━━━━━━  ENVIRONMENT  ━━━━━━━━━━━━━━━━━━
OPERATING SYSTEM : {os_name}

━━━━━━━━━━━━━━━━━━  INCIDENT  ━━━━━━━━━━━━━━━━━━
INCIDENT ID : {incident_id}

ROOT CAUSE:
{root_cause}

HEALING PLAN:
{healing_prompt}

SUGGESTED COMMANDS (for reference):
{cmds_block}

━━━━━━━━━━━━━━━━━━  YOUR TASK  ━━━━━━━━━━━━━━━━━━
Write a numbered list of step-by-step instructions the operator must follow
to resolve this incident manually. Each step must be:

  ① Concrete — tell the operator EXACTLY what to click, type, or navigate to.
  ② Platform-aware — use commands valid on {os_name}.
  ③ Self-contained — assume the operator knows nothing about this system.
  ④ Ordered — earlier steps must be done before later ones.

If the fix involves a web UI (GitHub, AWS, Docker Hub, etc.), describe
exactly which menu → sub-menu → button to use.

If a command must be run in the terminal, show the exact command.

End with a verification step the operator can use to confirm the fix worked.

Respond with ONLY the numbered instruction list — no preamble, no headers,
no extra commentary. Example format:

1. Go to https://github.com/<your-repo>/settings/secrets/actions
2. Click "New repository secret".
3. Set Name = DOCKER_PASSWORD, Value = <your Docker Hub access token>.
4. Click "Add secret".
5. Trigger a new workflow run: gh workflow run <workflow-name> --ref main
6. Verify: open the Actions tab and confirm the "docker/login-action" step shows ✓ green.
"""

    print(f"[LLMInstructor] Generating manual instructions for {incident_id}...")
    response = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.choices[0].message.content.strip()
    print(f"[LLMInstructor] Done — {len(raw.splitlines())} instruction lines generated.")
    return raw