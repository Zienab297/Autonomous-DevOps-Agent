"""
self_healing/self_healing_agent.py
------------------------------------
Receives a Solution from the Orchestrator (via Knowledge Agent).

Workflow:
    Solution received
         │
         ▼
    Step 0 — Instructions path: if files_to_modify is empty →
             call LLMInstructor → return INSTRUCTIONS_ONLY result
         │
         ▼
    Step 1 — Guard: ensure every files_to_modify entry has a path
         │
         ▼
    Step 2 — Snapshot: read each file from disk → store old_content
         │
         ▼
    Step 3 — LLM Fixer: call fix_files() → LLMFixResponse
         │
         ▼
    Step 4 — Validate LLM response
         │
         ▼
    Step 5 — Build FileModificationResult list
         │
         ▼
    Step 6 — Apply changes to disk (backed up first)
         │
         ▼
    Step 7 — Execute remediation commands
         │
         ▼
    Step 8 — Verify the fix
              │
         PASS ├──────────────────────────────► return SUCCESS
              │
         FAIL ├──► _rollback_files()
                   │
                   └──► return ROLLED_BACK
                         (orchestrator retries with Knowledge Agent)
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
import logging
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from agents.self_healing_agent.models import (
    LLMFixResponse, Solution, SelfHealingResult,
    CommandExecutionResult, FileModificationResult, FileToFix,
)
from core.models import RemediationStatus
from agents.self_healing_agent.llm_fixer      import fix_files
from agents.self_healing_agent.llm_instructor import generate_instructions
from agents.self_healing_agent.llm_verifier   import verify_fix, VerificationReport, VerificationStatus

logger = logging.getLogger(__name__)


# ── Backup directory ──────────────────────────────────────────────────────────
# Stored NEXT TO the project being monitored, NOT inside the devops-agent repo.
# project_root is injected by the Orchestrator when it creates the agent.
# Falls back to a sibling of the self_healing_agent directory if not set.
_DEFAULT_BACKUP_BASE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", ".self_healing_backups"
)


class SelfHealingAgent:
    """
    Receives a Solution, runs the LLM Fixer, validates the output,
    applies file changes, verifies the fix, and rolls back if verification fails.

    Parameters
    ----------
    apply_changes : if True, write new_content to disk after validation.
    project_root  : absolute path to the project being healed.
                    Backups are stored at <project_root>/.self_healing_backups/
                    If None, falls back to the devops-agent directory.
    """

    def __init__(self, apply_changes: bool = False, project_root: Optional[str] = None):
        self.apply_changes = apply_changes
        self.project_root  = project_root

        # Backup dir lives inside the TARGET project, not the agent repo.
        if project_root:
            self._backup_base = os.path.join(project_root, ".self_healing_backups")
        else:
            self._backup_base = os.path.normpath(_DEFAULT_BACKUP_BASE)

        logger.info(
            f"[SelfHealingAgent] Initialized — "
            f"apply_changes={self.apply_changes}, "
            f"backup_dir={self._backup_base}"
        )

    # ============================================================
    # Main Entry Point
    # ============================================================

    async def remediate(
        self,
        solution    : Solution,
        retry_count : int = 0,
    ) -> SelfHealingResult:
        """
        Full remediation pipeline for one Solution.

        Parameters
        ----------
        solution    : Solution produced by the Knowledge Agent.
        retry_count : How many previous attempts have been made for this
                      incident (passed through to SelfHealingResult so the
                      orchestrator can track the loop).

        Returns
        -------
        SelfHealingResult — status is one of:
            SUCCESS           → fix applied and verified
            INSTRUCTIONS_ONLY → no files to fix; human instructions printed
            ROLLED_BACK       → fix applied but verification failed; files restored
            FAILED            → guard / validation / LLM fixer error
        """
        logger.info(
            f"[SelfHealingAgent] Starting remediation for {solution.incident_id} "
            f"(retry #{retry_count})"
        )

        # ── step 0: instructions path — no files to auto-fix ─────────────
        if not solution.files_to_modify:
            logger.info(
                "[SelfHealingAgent] No files_to_modify — generating human instructions."
            )
            instructions = generate_instructions(
                incident_id        = solution.incident_id,
                root_cause         = solution.root_cause,
                healing_prompt     = solution.healing_prompt,
                suggested_commands = solution.suggested_commands,
            )
            return SelfHealingResult(
                incident_id   = solution.incident_id,
                status        = RemediationStatus.INSTRUCTIONS_ONLY,
                instructions  = instructions,
                retry_count   = retry_count,
            )

        # ── step 1: guard — every file entry must have a path ─────────────
        guard_errors = self._guard_solution(solution)
        if guard_errors:
            logger.warning(f"[SelfHealingAgent] Solution guard failed: {guard_errors}")
            return SelfHealingResult(
                incident_id     = solution.incident_id,
                status          = RemediationStatus.FAILED,
                validation_errors = guard_errors,
                retry_count     = retry_count,
            )

        # ── step 2: snapshot — read old content from disk ─────────────────
        self._snapshot_files(solution.files_to_modify)

        # ── step 3: call LLM fixer ────────────────────────────────────────
        logger.info("[SelfHealingAgent] Calling LLM Fixer...")
        try:
            llm_response: LLMFixResponse = fix_files(
                incident_id        = solution.incident_id,
                root_cause         = solution.root_cause,
                healing_prompt     = solution.healing_prompt,
                suggested_commands = solution.suggested_commands,
                files_to_modify    = solution.files_to_modify,
            )
        except Exception as e:
            logger.error(f"[SelfHealingAgent] LLM Fixer raised: {e}", exc_info=True)
            return SelfHealingResult(
                incident_id     = solution.incident_id,
                status          = RemediationStatus.FAILED,
                validation_errors = [f"LLM Fixer exception: {e}"],
                retry_count     = retry_count,
            )

        # ── step 4: validate LLM response ─────────────────────────────────
        known_paths = {f.path for f in solution.files_to_modify}
        validation_errors = self._validate_llm_response(llm_response, known_paths)
        if validation_errors:
            logger.warning(f"[SelfHealingAgent] Validation errors: {validation_errors}")
            return SelfHealingResult(
                incident_id     = solution.incident_id,
                status          = RemediationStatus.FAILED,
                validation_errors = validation_errors,
                llm_response    = llm_response,
                retry_count     = retry_count,
            )

        # ── step 5: build FileModificationResult list ─────────────────────
        snapshots = {f.path: f.current_content for f in solution.files_to_modify}
        file_modifications = self._build_modifications(
            llm_response.modified_files,
            snapshots,
        )

        # ── step 6: apply to disk ─────────────────────────────────────────
        if self.apply_changes:
            self._apply_to_disk(file_modifications, solution.incident_id)

        # ── step 7: execute remediation commands ──────────────────────────
        command_results: List[CommandExecutionResult] = []
        if self.apply_changes and llm_response.remediation_commands:
            logger.info(
                f"[SelfHealingAgent] Executing "
                f"{len(llm_response.remediation_commands)} remediation command(s)..."
            )
            command_results = self._execute_remediation_commands(
                llm_response.remediation_commands
            )

        # ── step 8: verify the fix ────────────────────────────────────────
        verification = None
        if self.apply_changes:
            verification = verify_fix(
                incident_id    = solution.incident_id,
                root_cause     = solution.root_cause,
                healing_prompt = solution.healing_prompt,
                modifications  = file_modifications,
            )
            logger.info(f"[SelfHealingAgent] Verification: {verification}")

            # ── step 8a: rollback if verification failed ──────────────────
            if verification.status == VerificationStatus.FAIL:
                logger.warning(
                    "[SelfHealingAgent] Verification FAILED — rolling back all file changes."
                )
                self._rollback_files(file_modifications)

                return SelfHealingResult(
                    incident_id                 = solution.incident_id,
                    status                      = RemediationStatus.ROLLED_BACK,
                    file_modifications          = file_modifications,
                    steps                       = llm_response.steps,
                    confidence                  = llm_response.confidence,
                    remediation_command_results = command_results,
                    llm_response                = llm_response,
                    verification                = verification,
                    retry_count                 = retry_count,
                )

        # ── determine final status ────────────────────────────────────────
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
            retry_count                 = retry_count,
        )

    # ============================================================
    # Step 1 — Guard
    # ============================================================

    def _guard_solution(self, solution: Solution) -> List[str]:
        errors = []
        for i, f in enumerate(solution.files_to_modify):
            tag = f"files_to_modify[{i}]"
            if not f.path.strip():
                errors.append(f"{tag} missing 'path'.")
        return errors

    # ============================================================
    # Step 2 — Snapshot
    # ============================================================

    def _snapshot_files(self, files_to_modify: List[FileToFix]) -> None:
        for f in files_to_modify:
            if os.path.isfile(f.path):
                try:
                    with open(f.path, "r", encoding="utf-8") as fh:
                        f.current_content = fh.read()
                    logger.debug(f"[SelfHealingAgent] Snapshot OK: {f.path}")
                except OSError as e:
                    logger.warning(f"[SelfHealingAgent] Could not read {f.path}: {e}")
                    f.current_content = ""
            else:
                logger.debug(f"[SelfHealingAgent] File not found (new file?): {f.path}")
                f.current_content = ""

    # ============================================================
    # Step 4 — Validate
    # ============================================================

    def _validate_llm_response(
        self,
        llm_response : LLMFixResponse,
        known_paths  : set,
    ) -> List[str]:
        errors = []
        if not llm_response.modified_files:
            errors.append("LLM returned zero modified_files entries.")
            return errors

        for i, entry in enumerate(llm_response.modified_files):
            tag  = f"modified_files[{i}]"
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

    def _apply_to_disk(
        self,
        modifications : List[FileModificationResult],
        incident_id   : str = "INC",
    ) -> None:
        """
        Write new_content to each file.

        Backup location (always INSIDE the target project):
            <project_root>/.self_healing_backups/
                INC-ABCD1234_20240318_120600/
                    requirements.txt.bak
                    docker-compose.yml.bak

        The incident_id is embedded in the folder name so multiple incidents
        never overwrite each other's backups.
        """
        timestamp  = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        backup_dir = os.path.join(
            self._backup_base,
            f"{incident_id}_{timestamp}",
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
                    logger.info(f"[SelfHealingAgent] Backup saved: {backup_path}")

                # ── write new content ─────────────────────────────────────
                os.makedirs(os.path.dirname(mod.path) or ".", exist_ok=True)
                write_mode = "a" if mod.action == "append" else "w"

                with open(mod.path, write_mode, encoding="utf-8") as fh:
                    fh.write(mod.new_content)

                mod.applied = True
                logger.info(f"[SelfHealingAgent] Applied ({mod.action}): {mod.path}")

            except OSError as e:
                mod.error = str(e)
                logger.error(f"[SelfHealingAgent] Failed to write {mod.path}: {e}")

    # ============================================================
    # Rollback — restore files from backup after failed verification
    # ============================================================

    def _rollback_files(self, modifications: List[FileModificationResult]) -> None:
        """
        Restore each file that was successfully applied to its original state
        by reading its .bak file and writing it back over the modified version.

        Called immediately after verify_fix() returns FAIL.

        For each FileModificationResult where applied=True and backup_path exists:
            1. Read backup_path  (the original content saved before the fix)
            2. Overwrite mod.path with that content
            3. Log the result

        Files that were NOT applied (mod.applied=False) are skipped — there is
        nothing to restore because the original was never overwritten.
        """
        logger.info("[SelfHealingAgent] Starting rollback...")

        for mod in modifications:
            if not mod.applied:
                logger.debug(f"[SelfHealingAgent] Skipping rollback for {mod.path} (not applied)")
                continue

            if not mod.backup_path or not os.path.isfile(mod.backup_path):
                # No backup file — restore from the in-memory old_content snapshot
                if mod.old_content:
                    try:
                        with open(mod.path, "w", encoding="utf-8") as fh:
                            fh.write(mod.old_content)
                        logger.info(
                            f"[SelfHealingAgent] Rolled back (from memory): {mod.path}"
                        )
                    except OSError as e:
                        logger.error(
                            f"[SelfHealingAgent] Could not roll back {mod.path}: {e}"
                        )
                else:
                    logger.warning(
                        f"[SelfHealingAgent] No backup or old_content for {mod.path} — "
                        f"cannot rollback, leaving as-is."
                    )
                continue

            # Restore from the .bak file on disk
            try:
                with open(mod.backup_path, "r", encoding="utf-8") as fh:
                    original_content = fh.read()

                with open(mod.path, "w", encoding="utf-8") as fh:
                    fh.write(original_content)

                mod.applied = False   # mark as rolled back
                logger.info(
                    f"[SelfHealingAgent] Rolled back (from .bak): "
                    f"{mod.path} ← {mod.backup_path}"
                )

            except OSError as e:
                logger.error(
                    f"[SelfHealingAgent] Rollback failed for {mod.path}: {e}"
                )

        logger.info("[SelfHealingAgent] Rollback complete.")

    # ============================================================
    # Step 7 — Execute Remediation Commands
    # ============================================================

    def _execute_remediation_commands(
        self,
        commands : List[Dict],
        timeout  : int = 120,
    ) -> List[CommandExecutionResult]:
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

            if abort_triggered:
                result.skipped = True
                logger.warning(f"[SelfHealingAgent] Skipping command (abort in effect): {command!r}")
                continue

            if not command:
                result.error   = "empty command string — skipped"
                result.skipped = True
                continue

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
                        logger.info(f"[SelfHealingAgent] Command succeeded (order={order}): {command!r}")
                        break

                    logger.warning(
                        f"[SelfHealingAgent] Command failed "
                        f"(order={order}, rc={result.returncode}): {command!r}"
                        + (f"\n  stderr: {result.stderr}" if result.stderr else "")
                    )

                except subprocess.TimeoutExpired:
                    result.error      = f"timed out after {timeout}s"
                    result.returncode = -1
                    break

                except Exception as exc:
                    result.error      = str(exc)
                    result.returncode = -1
                    break

            if not result.succeeded and on_failure == "abort":
                logger.error(
                    f"[SelfHealingAgent] on_failure=abort — "
                    f"halting remaining commands after order={order}."
                )
                abort_triggered = True

        return results