"""
Alert Agent - Approval Manager
Tracks pending approvals in memory and resolves them when
an engineer clicks Approve / Deny / Forward in Slack.

Flow:
  1. AlertAgent creates an ApprovalRequest and calls wait_for_approval().
  2. The Slack webhook receives the button click and calls:
       - resolve_approval()  → for Approve / Deny
       - forward_approval()  → for Forward (Team or Lead)
  3. On forward: original approval is closed, a NEW approval is created
     for the target channel. Only ONE forward hop is allowed.
  4. wait_for_approval() always waits on the LATEST approval_id,
     so forwarding is transparent to the AlertAgent.
"""

import asyncio
import logging
import uuid
from datetime import datetime

from models import ApprovalRequest, ApprovalStatus, ErrorEvent, RoutingDecision

logger = logging.getLogger(__name__)


class ApprovalManager:
    """
    Manages the lifecycle of approval requests including one-hop forwarding.

    Forward rule:
      - DevOps engineer can forward to Team or Lead — once only.
      - The forwarded-to person gets [Approve] [Deny] only — no Forward button.
      - AlertAgent is unaware of forwarding; it just waits for the final answer.
    """

    def __init__(
        self,
        timeout_seconds:  int = 300,
        team_channel:     str = "#devops-team",
        lead_channel:     str = "#devops-leads",
    ):
        """
        Args:
            timeout_seconds: How long to wait for any single approval step.
            team_channel:    Slack channel to forward to when engineer picks "Team".
            lead_channel:    Slack channel to forward to when engineer picks "Lead".
        """
        self.timeout_seconds = timeout_seconds
        self.team_channel    = team_channel
        self.lead_channel    = lead_channel

        # approval_id → (ApprovalRequest, asyncio.Event)
        self._pending: dict[str, tuple[ApprovalRequest, asyncio.Event]] = {}

        # original_approval_id → forwarded_approval_id
        # Used so wait_for_approval() can follow the chain transparently.
        self._forwarded_to: dict[str, str] = {}

    # ─────────────────────────────────────────
    # Creating approvals
    # ─────────────────────────────────────────

    def create_approval(
        self,
        event:         ErrorEvent,
        decision:      RoutingDecision,
        approval_type: str,          # "before_rag" | "before_apply"
        can_forward:   bool = True,  # False when this IS a forwarded approval
    ) -> ApprovalRequest:
        """
        Create a new ApprovalRequest and register it as pending.
        Returns the ApprovalRequest (caller sends it to Slack).
        """
        approval_id = str(uuid.uuid4())
        approval    = ApprovalRequest(
            approval_id=approval_id,
            event=event,
            decision=decision,
            approval_type=approval_type,
            can_forward=can_forward,
        )
        self._pending[approval_id] = (approval, asyncio.Event())

        logger.info(
            "Approval created [%s] type=%s service=%s can_forward=%s",
            approval_id, approval_type, event.service, can_forward,
        )
        return approval

    # ─────────────────────────────────────────
    # Waiting (called by AlertAgent — unaware of forwarding)
    # ─────────────────────────────────────────

    async def wait_for_approval(self, approval: ApprovalRequest) -> ApprovalStatus:
        """
        Wait for the engineer (or forwarded target) to respond.
        Transparently follows one forward hop if it happens.

        Returns ApprovalStatus.APPROVED, DENIED, or PENDING (timeout).
        """
        current_id = approval.approval_id
        status     = await self._wait_single(current_id)

        # If this approval was forwarded, wait on the new one instead
        if status == ApprovalStatus.FORWARDED:
            forwarded_id = self._forwarded_to.get(current_id)
            if forwarded_id:
                logger.info("Following forward hop: %s → %s", current_id, forwarded_id)
                status = await self._wait_single(forwarded_id)
            else:
                logger.error("Forward recorded but no target found for %s", current_id)
                return ApprovalStatus.PENDING

        return status

    async def _wait_single(self, approval_id: str) -> ApprovalStatus:
        """Wait for one specific approval_id to resolve."""
        if approval_id not in self._pending:
            raise ValueError(f"Unknown approval_id: {approval_id}")

        _, done_event = self._pending[approval_id]

        try:
            await asyncio.wait_for(done_event.wait(), timeout=self.timeout_seconds)
        except asyncio.TimeoutError:
            logger.warning("Approval timed out [%s]", approval_id)
            self._pending.pop(approval_id, None)
            return ApprovalStatus.PENDING

        updated, _ = self._pending.pop(approval_id, (None, None))
        return updated.status if updated else ApprovalStatus.PENDING

    # ─────────────────────────────────────────
    # Resolving (Approve / Deny)
    # ─────────────────────────────────────────

    def resolve_approval(
        self,
        approval_id: str,
        status:      ApprovalStatus,
        resolved_by: str,
    ) -> ApprovalRequest | None:
        """Called by the Slack webhook when engineer clicks Approve or Deny."""
        if approval_id not in self._pending:
            logger.warning("resolve_approval: unknown id %s", approval_id)
            return None

        approval, done_event = self._pending[approval_id]
        approval.status      = status
        approval.resolved_at = datetime.utcnow()
        approval.resolved_by = resolved_by

        logger.info("Approval resolved [%s] status=%s by=%s", approval_id, status, resolved_by)
        done_event.set()
        return approval

    # ─────────────────────────────────────────
    # Forwarding
    # ─────────────────────────────────────────

    def forward_approval(
        self,
        original_approval_id: str,
        target:               str,   # "team" | "lead"
        forwarded_by:         str,   # Slack user ID
    ) -> tuple[ApprovalRequest, str] | None:
        """
        Forward an approval to Team or Lead channel.
        Closes the original approval (status=FORWARDED) and creates a new one.

        Returns:
            (new_approval, target_channel) so the webhook can send it to Slack.
            None if the approval is not found or already forwarded.
        """
        if original_approval_id not in self._pending:
            logger.warning("forward_approval: unknown id %s", original_approval_id)
            return None

        original, done_event = self._pending[original_approval_id]

        # Guard: only one hop allowed
        if not original.can_forward:
            logger.warning("forward_approval: already forwarded, rejecting second hop")
            return None

        # Determine target channel
        target_channel = self.lead_channel if target == "lead" else self.team_channel

        # Close the original approval as FORWARDED
        original.status      = ApprovalStatus.FORWARDED
        original.resolved_at = datetime.utcnow()
        original.resolved_by = forwarded_by
        done_event.set()   # unblocks _wait_single, which sees FORWARDED and follows the chain

        # Create the new approval for the target (no Forward button this time)
        new_approval = self.create_approval(
            event=original.event,
            decision=original.decision,
            approval_type=original.approval_type,
            can_forward=False,   # ← one hop only
        )

        # Record the chain so wait_for_approval() can follow it
        self._forwarded_to[original_approval_id] = new_approval.approval_id

        logger.info(
            "Approval forwarded [%s] → [%s] target=%s by=%s",
            original_approval_id, new_approval.approval_id, target_channel, forwarded_by,
        )
        return new_approval, target_channel

    # ─────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────

    def get_pending(self, approval_id: str) -> ApprovalRequest | None:
        entry = self._pending.get(approval_id)
        return entry[0] if entry else None

    @property
    def pending_count(self) -> int:
        return len(self._pending)