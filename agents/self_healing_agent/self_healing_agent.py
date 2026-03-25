"""
self_healing/self_healing_agent.py
------------------------------------
Receives a Solution from the Orchestrator (via Knowledge Agent).

Workflow:
    Solution received
         │
         ▼
    Step 1 — Guard: ensure every files_to_modify entry has a path + content
         │
         ▼
    Step 2 — Snapshot: read each file from disk → store old_content
         │
         ▼
    Step 3 — LLM Fixer: call fix_files() → LLMFixResponse
         │
         ▼
    Step 4 — Validate: every modified_files entry must have
             path, new_content, and path must match a known file
         │
         ▼
    Step 5 — Build FileModificationResult list
             (path, old_content, new_content) for the caller
         │
         ▼
    Step 6 — Apply changes to disk (optional, gated by apply=True)
         │
         ▼
    Returns SelfHealingResult
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
import logging
import os
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from models import LLMFixResponse, RemediationStatus, Solution, SelfHealingResult, CommandExecutionResult, FileModificationResult, FileToFix
from llm_fixer import fix_files
from llm_verifier import verify_fix, VerificationReport, VerificationStatus

logger = logging.getLogger(__name__)

# ── constant ──────────────────────────────────────────────────────────────────
BACKUP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".self_healing_backups")


# ============================================================
# Self-Healing Agent
# ============================================================

class SelfHealingAgent:
    """
    Receives a Solution, runs the LLM Fixer, validates the output,
    and optionally applies the file changes to disk.

    Example:
        agent = SelfHealingAgent(apply_changes=True)
        result = await agent.remediate(solution)

        for mod in result.file_modifications:
            print(mod.path)
            print(mod.old_content)
            print(mod.new_content)
    """

    def __init__(self, apply_changes: bool = False):
        """
        Parameters
        ----------
        apply_changes : if True, write new_content to disk after validation.
                        Defaults to False (dry-run / audit mode).
        """
        self.apply_changes = apply_changes
        logger.info(
            f"[SelfHealingAgent] Initialized — "
            f"apply_changes={self.apply_changes}"
        )

    # ============================================================
    # Main Entry Point
    # ============================================================

    async def remediate(self, solution: Solution) -> SelfHealingResult:
        """
        Full remediation pipeline for one Solution.

        Parameters
        ----------
        solution : Solution produced by the Knowledge Agent

        Returns
        -------
        SelfHealingResult with per-file before/after content
        """
        logger.info(f"[SelfHealingAgent] Starting remediation for {solution.incident_id}")

        # ── step 1: guard — solution must have files ──────────────────────
        guard_errors = self._guard_solution(solution)
        if guard_errors:
            logger.warning(
                f"[SelfHealingAgent] Solution guard failed: {guard_errors}"
            )
            return SelfHealingResult(
                incident_id=solution.incident_id,
                status=RemediationStatus.FAILED,
                validation_errors=guard_errors,
            )

        # ── step 2: snapshot — read old content from disk ─────────────────
        self._snapshot_files(solution.files_to_modify)

        # ── step 3: call LLM fixer ────────────────────────────────────────
        logger.info(f"[SelfHealingAgent] Calling LLM Fixer...")
        try:
            llm_response: LLMFixResponse = fix_files(
                incident_id        = solution.incident_id,
                root_cause         = solution.root_cause,
                healing_prompt     = solution.healing_prompt,
                suggested_commands = solution.suggested_commands,
                files_to_modify    = solution.files_to_modify,  # List[FileToFix]
            )
        except Exception as e:
            logger.error(f"[SelfHealingAgent] LLM Fixer raised: {e}", exc_info=True)
            return SelfHealingResult(
                incident_id=solution.incident_id,
                status=RemediationStatus.FAILED,
                validation_errors=[f"LLM Fixer exception: {e}"],
            )

        # ── step 4: validate LLM response ─────────────────────────────────
        known_paths = {f.path for f in solution.files_to_modify}
        validation_errors = self._validate_llm_response(llm_response, known_paths)

        if validation_errors:
            logger.warning(
                f"[SelfHealingAgent] Validation errors: {validation_errors}"
            )
            return SelfHealingResult(
                incident_id=solution.incident_id,
                status=RemediationStatus.FAILED,
                validation_errors=validation_errors,
                llm_response=llm_response,
            )

        # ── step 5: build FileModificationResult list ─────────────────────
        # Build a snapshots dict from the FileToFix objects for _build_modifications
        snapshots = {f.path: f.current_content for f in solution.files_to_modify}
        file_modifications = self._build_modifications(
            llm_response.modified_files,
            snapshots,
        )

        # ── step 6: apply to disk (if enabled) ───────────────────────
        if self.apply_changes:
            self._apply_to_disk(file_modifications)

        # ── step 7: execute remediation commands (if enabled) ─────────────
        command_results: List[CommandExecutionResult] = []
        if self.apply_changes and llm_response.remediation_commands:
            logger.info(
                f"[SelfHealingAgent] Executing "
                f"{len(llm_response.remediation_commands)} remediation command(s)..."
            )
            command_results = self._execute_remediation_commands(
                llm_response.remediation_commands
            )

        # ── step 8: verify the fix ────────────────────────────────────
        verification = None
        if self.apply_changes:
            # only verify after real changes are written to disk
            verification = verify_fix(
                incident_id    = solution.incident_id,
                root_cause     = solution.root_cause,
                healing_prompt = solution.healing_prompt,
                modifications  = file_modifications,
            )
            logger.info(f"[SelfHealingAgent] Verification: {verification}")

        # A run is successful only if all files applied AND no abort-level
        # command failed (skipped commands count as failures too)
        files_ok = all(m.applied or not self.apply_changes for m in file_modifications)
        cmds_ok  = all(
            c.succeeded or c.on_failure != "abort"
            for c in command_results
        )
        final_status = (
            RemediationStatus.SUCCESS if (files_ok and cmds_ok)
            else RemediationStatus.FAILED
        )

        return SelfHealingResult(
            incident_id                 = solution.incident_id,
            status                      = final_status,
            file_modifications          = file_modifications,
            steps                       = llm_response.steps,
            confidence                  = llm_response.confidence,
            remediation_command_results = command_results,
            llm_response                = llm_response,
            verification                = verification,
        )

    # ============================================================
    # Step 1 — Guard
    # ============================================================

    def _guard_solution(self, solution: Solution) -> List[str]:
        """
        Ensure the Solution is actionable before hitting the LLM.
        Only a valid 'path' is required per entry — content is read from
        disk and action is decided by the LLM Fixer.

        Returns a list of error strings (empty = all good).
        """
        errors = []

        if not solution.files_to_modify:
            errors.append("Solution.files_to_modify is empty — nothing to fix.")
            return errors

        for i, f in enumerate(solution.files_to_modify):
            tag = f"files_to_modify[{i}]"
            if not f.path.strip():
                errors.append(f"{tag} missing 'path'.")

        return errors

    # ============================================================
    # Step 2 — Snapshot
    # ============================================================

    def _snapshot_files(self, files_to_modify: List[FileToFix]) -> None:
        """
        Read each file from disk and store its current content directly
        on the FileToFix object under current_content so the LLM Fixer
        can see the full file before deciding what action to take.

        Mutates each FileToFix in-place — no return value needed since
        Solution.files_to_modify holds the same objects.
        """
        for f in files_to_modify:
            if os.path.isfile(f.path):
                try:
                    with open(f.path, "r", encoding="utf-8") as fh:
                        f.current_content = fh.read()
                    logger.debug(f"[SelfHealingAgent] Snapshot OK: {f.path}")
                except OSError as e:
                    logger.warning(
                        f"[SelfHealingAgent] Could not read {f.path}: {e}"
                    )
                    f.current_content = ""
            else:
                logger.debug(
                    f"[SelfHealingAgent] File not found (new file?): {f.path}"
                )
                f.current_content = ""     # new file — nothing yet

    # ============================================================
    # Step 4 — Validate
    # ============================================================

    def _validate_llm_response(
        self,
        llm_response : LLMFixResponse,
        known_paths  : set,
    ) -> List[str]:
        """
        Validate every entry in llm_response.modified_files.

        Rules
        -----
        1. modified_files must not be empty.
        2. Every entry must have a non-empty 'path'.
        3. Every entry must have a non-empty 'new_content'.
        4. Every path must match one of the paths in the original Solution.

        Returns a list of error strings (empty = valid).
        """
        errors = []

        if not llm_response.modified_files:
            errors.append("LLM returned zero modified_files entries.")
            return errors

        for i, entry in enumerate(llm_response.modified_files):
            tag = f"modified_files[{i}]"

            path = entry.get("path", "").strip()
            if not path:
                errors.append(f"{tag} missing 'path'.")
                continue

            if not entry.get("new_content", "").strip():
                errors.append(f"{tag} (path={path}) missing 'new_content'.")

            if path not in known_paths:
                errors.append(
                    f"{tag} path='{path}' was not in the original "
                    f"files_to_modify. Known paths: {sorted(known_paths)}"
                )

        return errors

    # ============================================================
    # Step 5 — Build FileModificationResult
    # ============================================================

    def _build_modifications(
        self,
        modified_files : List[Dict],
        snapshots      : Dict[str, str],
    ) -> List[FileModificationResult]:
        """
        Merge LLM output + disk snapshots into FileModificationResult objects.

        old_content comes from the disk snapshot (via FileToFix.current_content).
        new_content comes from the LLM response.
        action comes from the LLM response (it decided based on the file content).
        """
        results = []
        for entry in modified_files:
            path = entry["path"].strip()
            results.append(
                FileModificationResult(
                    path        = path,
                    old_content = snapshots.get(path, ""),
                    new_content = entry.get("new_content", ""),
                    action      = entry.get("action", "overwrite"),
                )
            )
        return results

    # ============================================================
    # Step 6 — Apply to Disk
    # ============================================================

    def _apply_to_disk(self, modifications: List[FileModificationResult]) -> None:
        """
        Write new_content to each file according to its action.
        A timestamped backup of the original is saved before every write.

        Backup location:
            .self_healing_backups/
                INC-001_20240318_120600/
                    requirements.txt.bak
                    docker-compose.yml.bak
        """
        timestamp  = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        backup_dir = os.path.join(
            BACKUP_DIR,
            f"{modifications[0].path.split(os.sep)[-1]}_{timestamp}"
            if modifications else timestamp
        )

        for mod in modifications:
            try:
                # ── backup BEFORE touching the file ──────────────────────
                if mod.old_content:
                    os.makedirs(backup_dir, exist_ok=True)
                    backup_filename = os.path.basename(mod.path) + ".bak"
                    backup_path     = os.path.join(backup_dir, backup_filename)

                    with open(backup_path, "w", encoding="utf-8") as fh:
                        fh.write(mod.old_content)

                    mod.backup_path = backup_path
                    logger.info(
                        f"[SelfHealingAgent] Backup saved: {backup_path}"
                    )

                # ── write new content ─────────────────────────────────────
                os.makedirs(os.path.dirname(mod.path) or ".", exist_ok=True)
                write_mode = "a" if mod.action == "append" else "w"

                with open(mod.path, write_mode, encoding="utf-8") as fh:
                    fh.write(mod.new_content)

                mod.applied = True
                logger.info(
                    f"[SelfHealingAgent] Applied ({mod.action}): {mod.path}"
                )

            except OSError as e:
                mod.error = str(e)
                logger.error(
                    f"[SelfHealingAgent] Failed to write {mod.path}: {e}"
                )

    # ============================================================
    # Step 7 — Execute Remediation Commands
    # ============================================================

    def _execute_remediation_commands(
        self,
        commands : List[Dict],
        timeout  : int = 120,
    ) -> List[CommandExecutionResult]:
        """
        Execute the remediation commands produced by the LLM in order.

        Each command dict is expected to have:
            command     : str   — the shell command to run
            description : str   — human-readable explanation
            order       : int   — execution sequence (already sorted by llm_fixer)
            on_failure  : str   — "abort" | "continue" | "retry"

        Behaviour
        ---------
        - "abort"    : log the failure and mark all remaining commands as skipped.
        - "continue" : log the failure but keep executing subsequent commands.
        - "retry"    : run the command once more; if it fails again, treat as "abort".
        """
        results: List[CommandExecutionResult] = []
        abort_triggered = False

        for cmd_dict in commands:
            command     = cmd_dict.get("command", "").strip()
            description = cmd_dict.get("description", "")
            order       = int(cmd_dict.get("order", len(results) + 1))
            on_failure  = cmd_dict.get("on_failure", "abort").lower()

            result = CommandExecutionResult(
                command     = command,
                description = description,
                order       = order,
                on_failure  = on_failure,
            )
            results.append(result)

            # ── skip everything after an abort ────────────────────────────
            if abort_triggered:
                result.skipped = True
                logger.warning(
                    f"[SelfHealingAgent] Skipping command (abort in effect): {command!r}"
                )
                continue

            if not command:
                result.error   = "empty command string — skipped"
                result.skipped = True
                logger.warning(f"[SelfHealingAgent] Empty command at order={order}, skipping.")
                continue

            # ── run (with optional retry) ─────────────────────────────────
            attempts = 2 if on_failure == "retry" else 1

            for attempt in range(1, attempts + 1):
                logger.info(
                    f"[SelfHealingAgent] Running command "
                    f"(order={order}, attempt={attempt}/{attempts}): {command!r}"
                )
                try:
                    proc = subprocess.run(
                        command,
                        shell=True,
                        capture_output=True,
                        text=True,
                        timeout=timeout,
                    )
                    result.returncode = proc.returncode
                    result.stdout     = proc.stdout.strip()
                    result.stderr     = proc.stderr.strip()

                    if result.succeeded:
                        logger.info(
                            f"[SelfHealingAgent] Command succeeded "
                            f"(order={order}): {command!r}"
                        )
                        break

                    logger.warning(
                        f"[SelfHealingAgent] Command failed "
                        f"(order={order}, rc={result.returncode}): {command!r}"
                        + (f"\n  stderr: {result.stderr}" if result.stderr else "")
                    )

                except subprocess.TimeoutExpired:
                    result.error      = f"timed out after {timeout}s"
                    result.returncode = -1
                    logger.error(
                        f"[SelfHealingAgent] Command timed out "
                        f"(order={order}): {command!r}"
                    )
                    break

                except Exception as exc:
                    result.error      = str(exc)
                    result.returncode = -1
                    logger.error(
                        f"[SelfHealingAgent] Command raised exception "
                        f"(order={order}): {exc}",
                        exc_info=True,
                    )
                    break

            # ── decide whether to abort the remaining commands ─────────────
            if not result.succeeded and on_failure == "abort":
                logger.error(
                    f"[SelfHealingAgent] on_failure=abort — "
                    f"halting remaining commands after order={order}."
                )
                abort_triggered = True

        return results