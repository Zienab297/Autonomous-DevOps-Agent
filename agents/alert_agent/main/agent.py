"""
Alert Agent - Main Orchestrator

Full flow:
─────────────────────────────────────────────────────────────────
  ErrorEvent received
       │
       ▼
  RoutingEngine → RoutingDecision
       │
       ▼
  Send initial alert (Slack / Email / Call)
       │
       ▼
  [Approve to run RAG?]          ← Approval 1 (only for low/low/>6)
       │
       ▼
  RAG → Solution (text only: cause, fix, shell commands)
       │
       ▼
  Send solution summary to engineer
       │
       ▼
  [Approve to run Self-Healing?] ← Approval 2 (med / high / critical / low/>6)
       │
       ▼
  Self-Healing prepare → LLM fixes files → list[FileModification]
                         (nothing written to disk yet)
       │
       ▼
  Show Before/After per file to engineer
       │
       ▼
  [Approve to apply to disk?]    ← Approval 3 (always, when there are modifications)
       │
       ▼
  Self-Healing apply → write files to disk
       │
       ▼
  Send resolution message
─────────────────────────────────────────────────────────────────
"""

import logging
from typing import Callable, Awaitable

from models import (
    ErrorEvent, RoutingDecision, Solution, FileModification,
    AlertAction, ApprovalStatus, NotificationChannel,
)
from routing      import RoutingEngine
from notifications.slack import SlackProvider
from notifications.email import EmailProvider
from approval    import ApprovalManager

logger = logging.getLogger(__name__)

# RAG handler: receives the error event, returns a text-only Solution
RAGHandler = Callable[[ErrorEvent], Awaitable[Solution]]

# Self-Healing prepare: reads files, runs LLM fix loop, returns modifications — no disk write
SelfHealingPrepareHandler = Callable[[ErrorEvent, Solution], Awaitable[list[FileModification]]]

# Self-Healing apply: writes the engineer-approved modifications to disk
SelfHealingApplyHandler = Callable[[ErrorEvent, list[FileModification]], Awaitable[None]]


class AlertAgent:
    """
    Orchestrates the full incident pipeline.

    Usage:
        agent = AlertAgent(slack, email, approval_manager)
        agent.set_rag_handler(my_rag_fn)
        agent.set_self_healing_prepare_handler(my_prepare_fn)
        agent.set_self_healing_apply_handler(my_apply_fn)
        await agent.handle(event)
    """

    def __init__(
        self,
        slack:            SlackProvider,
        email:            EmailProvider,
        approval_manager: ApprovalManager,
    ):
        self.slack            = slack
        self.email            = email
        self.approval_manager = approval_manager
        self.routing_engine   = RoutingEngine()

        self._rag_handler:          RAGHandler | None                = None
        self._self_healing_prepare: SelfHealingPrepareHandler | None = None
        self._self_healing_apply:   SelfHealingApplyHandler | None   = None

    # ─────────────────────────────────────────
    # Handler injection
    # ─────────────────────────────────────────

    def set_rag_handler(self, handler: RAGHandler) -> None:
        """Inject the RAG / Knowledge Agent callable."""
        self._rag_handler = handler

    def set_self_healing_prepare_handler(self, handler: SelfHealingPrepareHandler) -> None:
        """
        Inject the Self-Healing prepare step.
        Runs LLM fix loop, returns FileModifications — nothing written to disk yet.
        """
        self._self_healing_prepare = handler

    def set_self_healing_apply_handler(self, handler: SelfHealingApplyHandler) -> None:
        """
        Inject the Self-Healing apply step.
        Writes engineer-approved modifications to disk.
        """
        self._self_healing_apply = handler

    # ─────────────────────────────────────────
    # Main entry point
    # ─────────────────────────────────────────

    async def handle(self, event: ErrorEvent) -> None:
        """Full incident pipeline. The only public method callers need."""
        logger.info(
            "AlertAgent.handle() — service=%s severity=%s impact=%s freq=%s timing=%s",
            event.service, event.severity, event.impact, event.frequency, event.timing,
        )

        # ── Step 1: Route ──────────────────────────────────────────
        decision = self.routing_engine.route(event)
        logger.info("Routing: action=%s", decision.action)

        # ── Step 2: Send initial alert ─────────────────────────────
        await self._send_initial_alert(event, decision)

        # ── Step 3: Auto-rollback (low/low/infrequent — skips all approvals) ──
        if decision.action == AlertAction.NOTIFY_AND_ROLLBACK:
            await self._trigger_rollback(event)
            return

        # ── Step 4: Approval 1 — approve to run RAG ───────────────
        if decision.requires_approval_before_rag:
            approved = await self._request_approval(event, decision, approval_type="before_rag")
            if not approved:
                return

        # ── Step 5: RAG → Solution (text only) ────────────────────
        solution = await self._run_rag(event)
        if solution is None:
            logger.warning("RAG returned no solution — stopping.")
            return

        # ── Step 6: Send solution summary (text only, no buttons yet) ──
        await self._send_solution_summary(event, solution, decision)

        # ── Step 7: Approval 2 — approve to run Self-Healing ───────
        if decision.requires_approval_before_healing:
            approved = await self._request_approval(event, decision, approval_type="before_healing")
            if not approved:
                return

        # ── Step 8: Self-Healing prepare — LLM fixes files, no disk write yet ──
        modifications = await self._prepare_fix(event, solution)
        if not modifications:
            logger.info("No file modifications produced — pipeline complete.")
            await self._send_resolution(event, solution.fix_commands, modifications=[])
            return

        # ── Step 9: Show Before/After per file to engineer ─────────
        self.slack.send_file_modifications(event, modifications)

        # ── Step 10: Approval 3 — approve to write files to disk ───
        approved = await self._request_approval(event, decision, approval_type="before_apply")
        if not approved:
            return

        # ── Step 11: Self-Healing apply — write approved files to disk ──
        await self._apply_fix(event, modifications)

        # ── Step 12: Resolution ────────────────────────────────────
        await self._send_resolution(event, solution.fix_commands, modifications)

    # ─────────────────────────────────────────
    # Private steps
    # ─────────────────────────────────────────

    async def _send_initial_alert(
        self, event: ErrorEvent, decision: RoutingDecision
    ) -> None:
        """Send the initial alert to Slack and Email. Phone call logged as placeholder."""
        self.slack.send_alert(event, decision)
        self.email.send_alert(event, decision)

        if NotificationChannel.PHONE_CALL in decision.channels:
            # Placeholder — phone call integration (e.g. PagerDuty) to be added
            logger.critical(
                "PHONE CALL REQUIRED — service=%s severity=%s",
                event.service, event.severity,
            )

    async def _request_approval(
        self,
        event:         ErrorEvent,
        decision:      RoutingDecision,
        approval_type: str,   # "before_rag" | "before_healing" | "before_apply"
    ) -> bool:
        """
        Send an approval request to Slack and wait for the engineer.
        Returns True if approved, False if denied or timed out.
        """
        approval      = self.approval_manager.create_approval(event, decision, approval_type)
        ts            = self.slack.send_approval_request(approval)
        approval.slack_ts = ts

        status = await self.approval_manager.wait_for_approval(approval)

        if status == ApprovalStatus.APPROVED:
            logger.info("Approval granted [%s] type=%s", approval.approval_id, approval_type)
            return True

        logger.info("Approval denied/timed out [%s] status=%s", approval.approval_id, status)
        self.slack.send_pipeline_stopped(event, approval_type, status)
        return False

    async def _trigger_rollback(self, event: ErrorEvent) -> None:
        """Low/low/infrequent — engineer has been notified, pipeline stops here."""
        logger.info(
            "Pipeline stopped — low severity / low impact / infrequent. "
            "Engineer notified via Slack. No RAG, no self-healing. service=%s",
            event.service,
        )

    async def _run_rag(self, event: ErrorEvent) -> Solution | None:
        """Run the RAG handler. Returns Solution (text only) or None."""
        if not self._rag_handler:
            logger.error("RAG handler not set.")
            return None
        logger.info("Running RAG for service=%s", event.service)
        return await self._rag_handler(event)

    async def _send_solution_summary(
        self,
        event:    ErrorEvent,
        solution: Solution,
        decision: RoutingDecision,
    ) -> None:
        """
        Send RAG solution summary — text only.
        No approval buttons here; Approval 2 is a separate message after this.
        """
        self.slack.send_solution_summary(event, solution)
        self.email.send_solution_summary(event, solution)

    async def _prepare_fix(
        self,
        event:    ErrorEvent,
        solution: Solution,
    ) -> list[FileModification]:
        """
        Self-Healing prepare step:
        LLM reads files, generates fixes, returns list[FileModification].
        Nothing written to disk here.
        """
        if not self._self_healing_prepare:
            logger.error("Self-healing prepare handler not set.")
            return []
        logger.info("Self-Healing preparing modifications for service=%s", event.service)
        return await self._self_healing_prepare(event, solution)

    async def _apply_fix(
        self,
        event:         ErrorEvent,
        modifications: list[FileModification],
    ) -> None:
        """Self-Healing apply step: write engineer-approved files to disk."""
        if not self._self_healing_apply:
            logger.error("Self-healing apply handler not set.")
            return
        logger.info(
            "Writing %d file(s) to disk for service=%s",
            len(modifications), event.service,
        )
        await self._self_healing_apply(event, modifications)

    async def _send_resolution(
        self,
        event:         ErrorEvent,
        fix_commands:  list[str],
        modifications: list[FileModification],
    ) -> None:
        """Send resolution message after everything is applied."""
        self.slack.send_resolution(event, fix_commands, modifications)
        self.email.send_resolution(event, fix_commands, modifications)
        logger.info("Resolution sent for service=%s", event.service)