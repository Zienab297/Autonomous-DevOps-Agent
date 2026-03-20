"""
run_flow.py
-----------
Reads the 4 broken config files from disk, builds one Solution per file
(with the BROKEN content as context), then passes each Solution into the
real SelfHealingAgent → real llm_fixer (Groq) → real llm_verifier.

Fixed files are written back to disk after the LLM generates the fixes.

Usage
-----
    # Run from the directory that contains your project files:
    python run_flow.py

    # Or point explicitly at a directory:
    python run_flow.py --base-dir /path/to/your/project

Project layout expected inside --base-dir:
    requirements_.txt
    ci.yml
    Dockerfile
    deployment.yaml

Place this file next to self_healing_agent.py, llm_fixer.py, llm_verifier.py
(or adjust sys.path below).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime
from typing import Dict, List
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
# ── logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# PATH SETUP
# All of self_healing_agent.py, llm_fixer.py, llm_verifier.py, and core/
# must live in the same folder as this script (or adjust the path below).
# ══════════════════════════════════════════════════════════════════════════════

AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, AGENT_DIR)

# If core/ lives one level up, uncomment:
# sys.path.insert(0, os.path.join(AGENT_DIR, ".."))


# ══════════════════════════════════════════════════════════════════════════════
# IMPORTS  (real modules — no stubs)
# ══════════════════════════════════════════════════════════════════════════════

from models import (           # noqa: E402
    Solution,
    RemediationStatus,
    VerificationStatus,
    SelfHealingResult,
)
from self_healing_agent import SelfHealingAgent  # noqa: E402


# ══════════════════════════════════════════════════════════════════════════════
# STEP 1 — Read broken files from disk
# ══════════════════════════════════════════════════════════════════════════════

def read_file(path: str) -> str:
    """Read a file and return its content. Raises clearly if missing."""
    if not os.path.isfile(path):
        raise FileNotFoundError(
            f"\nExpected file not found: {path}\n"
            f"Make sure --base-dir points to the folder containing your project files."
        )
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


# ══════════════════════════════════════════════════════════════════════════════
# STEP 2 — Build Solutions
#
# files_to_modify[].content = the ACTUAL broken file content read from disk.
# The LLM receives this as context and figures out the fix itself.
# Nothing is hardcoded here.
# ══════════════════════════════════════════════════════════════════════════════

def build_solutions(base_dir: str) -> List[Solution]:
    def p(filename: str) -> str:
        return os.path.join(base_dir, filename)

    solutions = []

    # ── 1. requirements_.txt ─────────────────────────────────────────────────
    req_path = p("requirements.txt")
    solutions.append(Solution(
        incident_id="INC-001",
        root_cause=(
            "requirements_.txt contains broken dependencies: "
            "a misspelled package name, a non-existent version number, "
            "and an unpinned package."
        ),
        healing_prompt=(
            "Audit requirements_.txt for:\n"
            "  1. Typos in package names (e.g. 'reqests' instead of 'requests')\n"
            "  2. Non-existent / impossible version numbers (e.g. torch==99.0.0)\n"
            "  3. Unpinned packages — every package must have ==<version>\n"
            "Produce the fully corrected requirements_.txt with all issues fixed."
        ),
        confidence=0.95,
        suggested_commands=[
            "pip install -r requirements_.txt",
            "pip check",
        ],
        references=["https://pypi.org/"],
        files_to_modify=[{
            "path":    req_path,
            "action":  "overwrite",
            "content": read_file(req_path),   # broken content — LLM fixes it
        }],
    ))

    # ── 2. ci.yml ────────────────────────────────────────────────────────────
    ci_path = p("ci.yml")
    solutions.append(Solution(
        incident_id="INC-002",
        root_cause=(
            "ci.yml has multiple defects: an unpinned GitHub Actions step, "
            "a wrong requirements filename, and a non-existent entry-point script."
        ),
        healing_prompt=(
            "Fix ci.yml:\n"
            "  1. Pin actions/checkout to a specific version tag (e.g. @v3)\n"
            "  2. Correct the requirements filename to match the actual file on disk "
            "(requirements_.txt)\n"
            "  3. Change the run script to an entry point that actually exists "
            "(use main.py if unsure)\n"
            "Produce the fully corrected ci.yml."
        ),
        confidence=0.95,
        suggested_commands=[],
        references=["https://github.com/actions/checkout"],
        files_to_modify=[{
            "path":    ci_path,
            "action":  "overwrite",
            "content": read_file(ci_path),    # broken content — LLM fixes it
        }],
    ))

    # ── 3. Dockerfile ────────────────────────────────────────────────────────
    docker_path = p("Dockerfile")
    solutions.append(Solution(
        incident_id="INC-003",
        root_cause=(
            "Dockerfile has an apt-get install with no package arguments "
            "(crashes the build) and Flask binds to 127.0.0.1 by default "
            "(container unreachable from outside)."
        ),
        healing_prompt=(
            "Fix the Dockerfile:\n"
            "  1. Replace the broken apt-get install line — add apt-get update, "
            "at least one real package, --no-install-recommends, and cache cleanup\n"
            "  2. Add --host=0.0.0.0 to the flask run CMD\n"
            "Produce the fully corrected Dockerfile."
        ),
        confidence=0.93,
        suggested_commands=[
            "docker build -t my-app:fixed .",
            "docker run --rm -p 5000:5000 my-app:fixed",
        ],
        references=["https://docs.docker.com/engine/reference/builder/"],
        files_to_modify=[{
            "path":    docker_path,
            "action":  "overwrite",
            "content": read_file(docker_path),   # broken content — LLM fixes it
        }],
    ))

    # ── 4. deployment.yaml ───────────────────────────────────────────────────
    deploy_path = p("deployment.yaml")
    solutions.append(Solution(
        incident_id="INC-004",
        root_cause=(
            "Kubernetes Deployment (apps/v1) is missing the required "
            "spec.selector field — kubectl apply will reject it with "
            "'spec.selector: Required value'."
        ),
        healing_prompt=(
            "Fix deployment.yaml:\n"
            "  1. Add the missing spec.selector.matchLabels block so it "
            "matches the pod template labels (app: my-app)\n"
            "Produce the fully corrected deployment.yaml."
        ),
        confidence=0.99,
        suggested_commands=[
            "kubectl apply -f deployment.yaml --dry-run=client",
            "kubectl apply -f deployment.yaml",
        ],
        references=["https://kubernetes.io/docs/concepts/workloads/controllers/deployment/"],
        files_to_modify=[{
            "path":    deploy_path,
            "action":  "overwrite",
            "content": read_file(deploy_path),   # broken content — LLM fixes it
        }],
    ))

    return solutions


# ══════════════════════════════════════════════════════════════════════════════
# STEP 3 — Pretty-print result
# ══════════════════════════════════════════════════════════════════════════════

W = 72
def _div(ch="─"): print(ch * W)

STATUS_ICON = {
    RemediationStatus.SUCCESS: "✅  SUCCESS",
    RemediationStatus.FAILED:  "❌  FAILED",
    RemediationStatus.PENDING: "⏳  PENDING",
}
VERIFY_ICON = {
    VerificationStatus.PASS:    "✅  PASS",
    VerificationStatus.FAIL:    "❌  FAIL",
    VerificationStatus.UNKNOWN: "❓  UNKNOWN",
}


def print_result(sol: Solution, result: SelfHealingResult) -> None:
    _div("═")
    print(f"  INCIDENT   : {sol.incident_id}")
    print(f"  STATUS     : {STATUS_ICON.get(result.status, str(result.status))}")
    print(f"  CONFIDENCE : {result.confidence:.0%}")
    _div()

    if result.validation_errors:
        print("  VALIDATION ERRORS:")
        for e in result.validation_errors:
            print(f"    ✗ {e}")
        print()
        return

    if result.steps:
        print("  STEPS (from LLM):")
        for i, s in enumerate(result.steps, 1):
            print(f"    {i}. {s}")
        print()

    if result.file_modifications:
        print("  FILE MODIFICATIONS:")
        for mod in result.file_modifications:
            tag = "applied ✓" if mod.applied else "not written"
            print(f"    [{mod.action.upper()}] {os.path.basename(mod.path)}  → {tag}")
            if mod.backup_path:
                print(f"      backup  : {mod.backup_path}")
            if mod.error:
                print(f"      error   : {mod.error}")
            # Mini diff — first 3 changed lines
            old_lines = (mod.old_content or "").splitlines()
            new_lines = (mod.new_content or "").splitlines()
            changes = [
                (i + 1, ol, nl)
                for i, (ol, nl) in enumerate(zip(old_lines, new_lines))
                if ol.strip() != nl.strip()
            ][:3]
            if changes:
                print("      diff preview:")
                for lineno, old, new in changes:
                    print(f"        line {lineno}  -  {old.strip()}")
                    print(f"        line {lineno}  +  {new.strip()}")
        print()

    if result.remediation_command_results:
        print("  REMEDIATION COMMANDS:")
        for cr in result.remediation_command_results:
            icon = "⏭ " if cr.skipped else ("✓ " if cr.succeeded else "✗ ")
            print(f"    {icon}[{cr.order}] {cr.command}")
            if cr.stderr:
                print(f"        stderr : {cr.stderr[:120]}")
        print()

    if result.verification:
        v = result.verification
        print("  VERIFICATION:")
        print(f"    status     : {VERIFY_ICON.get(v.status, str(v.status))}")
        print(f"    reason     : {v.reason}")
        print(f"    confidence : {v.confidence:.0%}")
        print()


# ══════════════════════════════════════════════════════════════════════════════
# STEP 4 — Main async flow
# ══════════════════════════════════════════════════════════════════════════════

async def run(base_dir: str) -> None:
    _div("═")
    print("  SELF-HEALING AGENT — Real LLM Flow (Groq)")
    print(f"  base_dir      : {base_dir}")
    print(f"  apply_changes : True  (files will be written to disk)")
    _div("═")

    agent     = SelfHealingAgent(apply_changes=True)
    solutions = build_solutions(base_dir)

    print(f"\n  {len(solutions)} solution(s) queued.\n")

    passed = failed = 0

    for sol in solutions:
        logger.info(f"── Remediating {sol.incident_id}: {sol.root_cause[:70]}…")
        result: SelfHealingResult = await agent.remediate(sol)
        print_result(sol, result)

        if result.status == RemediationStatus.SUCCESS:
            passed += 1
        else:
            failed += 1

    _div("═")
    print(f"  SUMMARY  ·  ✅ {passed} passed   ❌ {failed} failed   "
          f"out of {len(solutions)} total")
    _div("═")
    print()


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Self-Healing Agent — real Groq LLM flow"
    )
    parser.add_argument(
        "--base-dir",
        default=os.path.dirname(os.path.abspath(__file__)),
        help=(
            "Directory containing requirements_.txt, ci.yml, "
            "Dockerfile, deployment.yaml  "
            "(default: same folder as this script)"
        ),
    )
    args = parser.parse_args()
    asyncio.run(run(base_dir=args.base_dir))