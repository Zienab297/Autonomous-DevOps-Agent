"""
core/approval_manager.py
────────────────────────
Manages approvals across CLI and Email simultaneously.
First channel to respond wins — the other is cancelled.

CHANNELS:
  CLI    — always active; pauses the monitoring dashboard while waiting.
  Email  — active if EmailClient is injected; sends HTML email with
           Approve / Deny links served by ApprovalServer.

RESOLUTION:
  resolve_approval() is called by:
    * ApprovalServer  — when engineer clicks email link (GET /approve or /deny)
    * _cli_approval() — when user types yes/no in the terminal

DASHBOARD PAUSE:
  ApprovalManager accepts an optional `registry` reference.
  Before showing the CLI prompt it pauses the MonitoringAgent live dashboard.
"""

import asyncio
import logging
import uuid
from typing import Optional

logger = logging.getLogger(__name__)


class ApprovalManager:

    def __init__(
        self,
        email=None,
        timeout_seconds: int = 300,
        registry=None,
    ):
        self.email           = email
        self.timeout_seconds = timeout_seconds
        self.registry        = registry
        # approval_id -> (approved: bool | None, asyncio.Event)
        self._pending: dict = {}

    # ── Dashboard pause / resume ──────────────────────────────────────────────

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

    # ── Resolution (called by ApprovalServer) ────────────────────────────────

    def resolve_approval(self, approval_id: str, approved: bool, source: str = "unknown") -> Optional[bool]:
        """
        Resolve a pending approval. Returns the decision on first call,
        None if already resolved by another channel.
        """
        entry = self._pending.get(approval_id)
        if entry is None:
            return None
        _, done_event = entry
        if done_event.is_set():
            return None
        self._pending[approval_id] = (approved, done_event)
        done_event.set()
        logger.info("[ApprovalManager] Resolved id=%s approved=%s source=%s", approval_id, approved, source)
        return approved

    # ── Main entry point ──────────────────────────────────────────────────────

    async def request_approval(self, title: str, details: list = None, context: dict = None) -> bool:
        """Request approval from CLI + Email simultaneously. Returns True/False."""
        details     = details or []
        context     = context or {}
        approval_id = str(uuid.uuid4())
        done_event  = asyncio.Event()
        self._pending[approval_id] = (None, done_event)

        logger.info("[ApprovalManager] Requesting approval: %s", title)

        tasks = [
            asyncio.create_task(
                self._cli_approval(approval_id, title, details),
                name=f"approval-cli-{approval_id[:8]}",
            )
        ]
        if self.email:
            tasks.append(asyncio.create_task(
                self._email_approval(approval_id, title, details),
                name=f"approval-email-{approval_id[:8]}",
            ))

        try:
            await asyncio.wait_for(done_event.wait(), timeout=self.timeout_seconds)
        except asyncio.TimeoutError:
            logger.warning("[ApprovalManager] Approval timed out: %s", title)
            self.resolve_approval(approval_id, False, source="timeout")

        for task in tasks:
            if not task.done():
                task.cancel()

        entry    = self._pending.pop(approval_id, (False, None))
        approved = entry[0] if entry[0] is not None else False
        print(f"\n  [Approval] {'APPROVED' if approved else 'DENIED'}\n")
        return approved

    # ── CLI channel ───────────────────────────────────────────────────────────

    async def _cli_approval(self, approval_id: str, title: str, details: list) -> None:
        try:
            self._pause_monitoring()
            print(f"\n{'=' * 55}")
            print(f"  APPROVAL REQUIRED")
            print(f"{'-' * 55}")
            print(f"  {title}")
            if details:
                print(f"{'-' * 55}")
                for item in details:
                    print(f"    + {item}")
            print(f"{'-' * 55}")
            if self.email:
                print(f"  Also waiting on: Email")
                print(f"{'-' * 55}")

            answer = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: input("  Approve? [yes/no]: ").strip().lower(),
            )
            self.resolve_approval(approval_id, answer in ("yes", "y"), source="CLI")

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("[ApprovalManager] CLI error: %s", exc)
        finally:
            self._resume_monitoring()

    # ── Email channel ─────────────────────────────────────────────────────────

    async def _email_approval(self, approval_id: str, title: str, details: list) -> None:
        """Send email then wait for ApprovalServer to call resolve_approval()."""
        try:
            await self.email.send_approval_request(
                approval_id=approval_id,
                title=title,
                details=details,
            )
            _, done_event = self._pending.get(approval_id, (None, None))
            if done_event:
                await done_event.wait()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("[ApprovalManager] Email error: %s", exc)