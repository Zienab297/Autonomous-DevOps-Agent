"""
core/approval_manager.py
────────────────────────
Manages approvals across CLI, Slack, and Email simultaneously.
First channel to respond wins — others are cancelled.

GATE MAP (from orchestrator):
  Gate 1  — Scaffold complete → proceed to CI/CD?
  Gate 2  — CI/CD done → run Monitoring Agent?
  Gate 3  — Incident detected → run Knowledge Agent?
  Gate 4  — Solution found → apply to disk? (self-healing)
  Gate 5  — New solution found (retry #N) → apply?
  Gate 6  — Rolled back → re-invoke Knowledge Agent?

All six gates call request_approval() — the same interface as before.
The orchestrator never needs to know which channel responded.

CHANNEL BEHAVIOUR:
  CLI    — always active; pauses the monitoring dashboard while waiting.
  Slack  — active if SlackClient is injected; sends Block Kit buttons.
  Email  — active if EmailClient is injected; sends HTML email with links.

RESOLUTION:
  resolve_approval() is called by:
    • ApprovalServer  — for Slack button clicks and email link clicks
    • _cli_approval() — directly, when the user types yes/no

DASHBOARD PAUSE:
  ApprovalManager accepts an optional `registry` reference.
  Before showing the CLI prompt it calls monitoring_agent.pause_dashboard()
  and resumes after the answer is received — same pattern as before.

Usage (unchanged from old approval_manager):
    approved = await manager.request_approval(
        title="Scaffold complete — proceed to CI/CD?",
        details=["Dockerfile", "k8s/deployment.yaml"],
    )
"""

import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class ApprovalManager:
    """
    Sends approval requests to CLI + Slack + Email simultaneously.
    First channel to respond wins.
    """

    def __init__(
        self,
        slack=None,              # core.slack_client.SlackClient    (optional)
        email=None,              # core.email_client.EmailClient    (optional)
        timeout_seconds: int = 300,
        registry=None,           # AgentRegistry — used to pause monitoring dashboard
    ):
        self.slack           = slack
        self.email           = email
        self.timeout_seconds = timeout_seconds
        self.registry        = registry

        # Maps approval_id → (approved: bool, asyncio.Event)
        # Populated per-request; cleaned up after resolution.
        self._pending: dict[str, tuple[Optional[bool], asyncio.Event]] = {}

    # ── Pause / resume monitoring dashboard ──────────────────────────────────

    def _pause_monitoring(self) -> None:
        if not self.registry:
            return
        agent = self.registry.get_agent("monitoring_agent")
        if agent and hasattr(agent, "pause_dashboard"):
            try:
                agent.pause_dashboard()
            except Exception:
                pass

    def _resume_monitoring(self) -> None:
        if not self.registry:
            return
        agent = self.registry.get_agent("monitoring_agent")
        if agent and hasattr(agent, "resume_dashboard"):
            try:
                agent.resume_dashboard()
            except Exception:
                pass

    # ── Resolution (called by ApprovalServer and _cli_approval) ──────────────

    def resolve_approval(
        self,
        approval_id: str,
        approved:    bool,
        source:      str = "unknown",
    ) -> Optional[bool]:
        """
        Resolve a pending approval.

        Called by:
          • ApprovalServer._handle_slack()         — Slack button click
          • ApprovalServer._handle_email_click()   — email link click
          • _cli_approval()                        — terminal input

        Returns the approved value if this was the FIRST resolution,
        or None if the approval was already resolved by another channel.
        """
        entry = self._pending.get(approval_id)
        if entry is None:
            # Already resolved (another channel won the race) — ignore
            return None

        _, done_event = entry
        if done_event.is_set():
            # Race condition: event already set, ignore
            return None

        # Store the decision and signal all waiters
        self._pending[approval_id] = (approved, done_event)
        done_event.set()

        logger.info(
            "[ApprovalManager] Resolved approval_id=%s approved=%s source=%s",
            approval_id, approved, source,
        )
        return approved

    # ── Main entry point (unchanged interface) ────────────────────────────────

    async def request_approval(
        self,
        title:   str,
        details: list[str] = None,
        context: dict      = None,
    ) -> bool:
        """
        Request approval from all configured channels simultaneously.
        Returns True (approved) or False (denied / timeout).

        This is the ONLY method the orchestrator calls — interface unchanged.
        """
        import uuid
        details = details or []
        context = context or {}

        approval_id = str(uuid.uuid4())
        done_event  = asyncio.Event()
        self._pending[approval_id] = (None, done_event)

        logger.info("[ApprovalManager] Requesting approval: %s", title)

        # Build concurrent tasks — one per active channel
        tasks = []

        # 1. CLI (always)
        tasks.append(asyncio.create_task(
            self._cli_approval(approval_id, title, details),
            name=f"approval-cli-{approval_id[:8]}",
        ))

        # 2. Slack (if configured)
        if self.slack:
            tasks.append(asyncio.create_task(
                self._slack_approval(approval_id, title, details),
                name=f"approval-slack-{approval_id[:8]}",
            ))

        # 3. Email (if configured)
        if self.email:
            tasks.append(asyncio.create_task(
                self._email_approval(approval_id, title, details),
                name=f"approval-email-{approval_id[:8]}",
            ))

        # Wait for the first channel to respond, or timeout
        try:
            await asyncio.wait_for(done_event.wait(), timeout=self.timeout_seconds)
        except asyncio.TimeoutError:
            logger.warning("[ApprovalManager] Approval timed out: %s", title)
            self.resolve_approval(approval_id, False, source="timeout")

        # Cancel all remaining tasks (losers)
        for task in tasks:
            if not task.done():
                task.cancel()

        # Read and clean up the decision
        entry   = self._pending.pop(approval_id, (False, None))
        approved = entry[0] if entry[0] is not None else False
        source   = "resolved"

        print(
            f"\n  [Approval] {'✅ APPROVED' if approved else '❌ DENIED'}\n"
        )
        return approved

    # ── CLI approval ──────────────────────────────────────────────────────────

    async def _cli_approval(
        self,
        approval_id: str,
        title:       str,
        details:     list[str],
    ) -> None:
        """
        Show the approval prompt in the terminal.
        Pauses the monitoring dashboard before printing so background
        redraws don't overwrite what the user is typing.
        """
        try:
            self._pause_monitoring()

            print(f"\n{'═' * 55}")
            print(f"  APPROVAL REQUIRED")
            print(f"{'─' * 55}")
            print(f"  {title}")
            if details:
                print(f"{'─' * 55}")
                for item in details:
                    print(f"    + {item}")
            print(f"{'─' * 55}")
            if self.slack or self.email:
                channels = []
                if self.slack:
                    channels.append("Slack")
                if self.email:
                    channels.append("Email")
                print(f"  Also waiting on: {' / '.join(channels)}")
                print(f"{'─' * 55}")

            answer = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: input("  Approve? [yes/no]: ").strip().lower(),
            )

            approved = answer in ("yes", "y")
            self.resolve_approval(approval_id, approved, source="CLI")

        except asyncio.CancelledError:
            pass   # another channel won — that's expected
        except Exception as exc:
            logger.error("[ApprovalManager] CLI error: %s", exc)
        finally:
            self._resume_monitoring()

    # ── Slack approval ────────────────────────────────────────────────────────

    async def _slack_approval(
        self,
        approval_id: str,
        title:       str,
        details:     list[str],
    ) -> None:
        """
        Send an approval request to Slack and wait for a button click.

        The actual resolution happens when ApprovalServer receives the
        POST /slack/interactive callback and calls resolve_approval().
        This coroutine just waits on the done_event after posting.
        """
        try:
            if not self.slack:
                return

            await self.slack.send_approval_request(
                approval_id=approval_id,
                title=title,
                details=details,
            )

            # Wait for ApprovalServer to call resolve_approval()
            _, done_event = self._pending.get(approval_id, (None, None))
            if done_event:
                await done_event.wait()

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("[ApprovalManager] Slack error: %s", exc)

    # ── Email approval ────────────────────────────────────────────────────────

    async def _email_approval(
        self,
        approval_id: str,
        title:       str,
        details:     list[str],
    ) -> None:
        """
        Send an approval email and wait for a link click.

        The actual resolution happens when ApprovalServer receives the
        GET /approve or GET /deny request and calls resolve_approval().
        This coroutine just waits on the done_event after sending.
        """
        try:
            if not self.email:
                return

            await self.email.send_approval_request(
                approval_id=approval_id,
                title=title,
                details=details,
            )

            # Wait for ApprovalServer to call resolve_approval()
            _, done_event = self._pending.get(approval_id, (None, None))
            if done_event:
                await done_event.wait()

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("[ApprovalManager] Email error: %s", exc)