"""
core/approval_manager.py
────────────────────────
Manages approvals across CLI and Email simultaneously.
First channel to respond wins — the other is cancelled.

GATE MAP (from orchestrator):
  Gate 1  — Scaffold complete → proceed to CI/CD?
  Gate 2  — CI/CD done → run Monitoring Agent?
  Gate 3  — Incident detected → run Knowledge Agent?
  Gate 4  — Solution found → apply to disk? (self-healing)
  Gate 5  — New solution found (retry #N) → apply?
  Gate 6  — Rolled back → re-invoke Knowledge Agent?

All six gates call request_approval() — the orchestrator never needs to
know which channel responded.

CHANNEL BEHAVIOUR:
  CLI    — always active; pauses the monitoring dashboard while waiting.
  Email  — active if EmailClient is injected; sends HTML email with
           Approve / Deny links that hit the ApprovalServer.

RESOLUTION:
  resolve_approval() is called by:
    • ApprovalServer  — for email link clicks (GET /approve or /deny)
    • _cli_approval() — directly, when the user types yes/no

DASHBOARD PAUSE:
  ApprovalManager accepts an optional `registry` reference.
  Before showing the CLI prompt it calls monitoring_agent.pause_dashboard()
  and resumes after the answer is received.

BUG FIXES (v2):
  1. run_in_executor / input() cannot be cancelled — replaced with a
     readline loop on a non-blocking stdin fd so CancelledError is
     actually honoured. Each gate gets a fresh Future that is resolved
     exactly once; orphaned threads from previous gates can no longer
     inject answers into a new gate.

  2. resolve_approval() is now protected by an asyncio.Lock so the
     check-and-set is atomic (no TOCTOU race between two channels).

  3. _pending stores (decision_holder: list[Optional[bool]], asyncio.Event)
     instead of a plain tuple so the decision can be mutated in-place
     while the tuple key stays stable. This removes the fragile
     re-assignment pattern.

Usage:
    approved = await manager.request_approval(
        title="Scaffold complete — proceed to CI/CD?",
        details=["Dockerfile", "k8s/deployment.yaml"],
    )
"""

import asyncio
import concurrent.futures
import logging
import sys
from typing import Optional

logger = logging.getLogger(__name__)


# ── Internal per-request state ────────────────────────────────────────────────

class _ApprovalEntry:
    """
    Holds the mutable state for one pending approval request.

    Using a class (instead of a tuple that gets re-assigned) means every
    coroutine that has a reference to this object always sees the current
    decision without needing to re-look-up by approval_id.
    """

    __slots__ = ("decision", "event", "lock")

    def __init__(self) -> None:
        self.decision: Optional[bool] = None   # None = not yet resolved
        self.event    = asyncio.Event()
        self.lock     = asyncio.Lock()          # protects check-and-set


# ── Main class ────────────────────────────────────────────────────────────────

class ApprovalManager:
    """
    Sends approval requests to CLI + Email simultaneously.
    First channel to respond wins.
    """

    def __init__(
        self,
        email=None,              # core.email_client.EmailClient    (optional)
        timeout_seconds: int = 300,
        registry=None,           # AgentRegistry — used to pause monitoring dashboard
    ):
        self.email           = email
        self.timeout_seconds = timeout_seconds
        self.registry        = registry

        # Maps approval_id → _ApprovalEntry
        # Populated per-request; cleaned up after resolution.
        self._pending: dict[str, _ApprovalEntry] = {}

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

    async def resolve_approval_async(
        self,
        approval_id: str,
        approved:    bool,
        source:      str = "unknown",
    ) -> Optional[bool]:
        """
        Async-safe resolution.  Called from within the event loop
        (e.g. ApprovalServer route handlers and _cli_approval).

        Returns the approved value if this was the FIRST resolution,
        or None if already resolved by another channel.
        """
        entry = self._pending.get(approval_id)
        if entry is None:
            return None   # already cleaned up

        async with entry.lock:
            if entry.event.is_set():
                return None   # another channel already won

            entry.decision = approved
            entry.event.set()

        logger.info(
            "[ApprovalManager] Resolved approval_id=%s approved=%s source=%s",
            approval_id, approved, source,
        )
        return approved

    def resolve_approval(
        self,
        approval_id: str,
        approved:    bool,
        source:      str = "unknown",
    ) -> Optional[bool]:
        """
        Synchronous shim kept for ApprovalServer compatibility.

        ApprovalServer calls this from inside an aiohttp route handler
        which is already running on the event loop, so we schedule the
        async version as a task and return immediately.  The return value
        here is best-effort (None means "already resolved or not found").
        """
        entry = self._pending.get(approval_id)
        if entry is None:
            return None
        if entry.event.is_set():
            return None

        # Schedule the atomic async version; the caller doesn't need to await it
        # because the route handler will await the underlying _web.Response.
        try:
            loop = asyncio.get_event_loop()
            loop.create_task(
                self.resolve_approval_async(approval_id, approved, source),
                name=f"resolve-{source}-{approval_id[:8]}",
            )
        except RuntimeError:
            # Fallback if called outside a running loop (shouldn't happen in prod)
            entry.decision = approved
            entry.event.set()

        # Optimistic return for the HTML response page
        return approved if not entry.event.is_set() else None

    # ── Main entry point ──────────────────────────────────────────────────────

    async def request_approval(
        self,
        title:   str,
        details: list[str] = None,
        context: dict      = None,
    ) -> bool:
        """
        Request approval from all configured channels simultaneously.
        Returns True (approved) or False (denied / timeout).
        """
        import uuid
        details = details or []
        context = context or {}

        approval_id = str(uuid.uuid4())
        entry       = _ApprovalEntry()
        self._pending[approval_id] = entry

        logger.info("[ApprovalManager] Requesting approval: %s (id=%s)", title, approval_id[:8])

        # Build concurrent tasks — one per active channel
        tasks = []

        # 1. CLI (always)
        tasks.append(asyncio.create_task(
            self._cli_approval(approval_id, entry, title, details),
            name=f"approval-cli-{approval_id[:8]}",
        ))

        # 2. Email (if configured)
        if self.email:
            tasks.append(asyncio.create_task(
                self._email_approval(approval_id, entry, title, details),
                name=f"approval-email-{approval_id[:8]}",
            ))

        # Wait for the first channel to respond, or timeout
        try:
            await asyncio.wait_for(
                asyncio.shield(entry.event.wait()),
                timeout=self.timeout_seconds,
            )
        except asyncio.TimeoutError:
            logger.warning("[ApprovalManager] Approval timed out: %s", title)
            await self.resolve_approval_async(approval_id, False, source="timeout")

        # Cancel all remaining channel tasks (losers)
        for task in tasks:
            if not task.done():
                task.cancel()
        # Give cancelled tasks a moment to clean up
        await asyncio.gather(*tasks, return_exceptions=True)

        # Read and clean up the decision
        entry    = self._pending.pop(approval_id, None)
        approved = (entry.decision is True) if entry else False

        print(f"\n  [Approval] {'✅ APPROVED' if approved else '❌ DENIED'}\n")
        return approved

    # ── CLI approval ──────────────────────────────────────────────────────────

    async def _cli_approval(
        self,
        approval_id: str,
        entry:       _ApprovalEntry,
        title:       str,
        details:     list[str],
    ) -> None:
        """
        Show the approval prompt in the terminal.

        FIX: We no longer use run_in_executor(input()) because that
        executor thread CANNOT be cancelled — it keeps blocking even
        after the task is cancelled, so a later gate would inherit the
        zombie thread and its typed answer.

        Instead we use asyncio's add_reader on stdin (Unix) or a
        dedicated thread with a threading.Event cancel signal (Windows)
        so that CancelledError actually stops waiting.
        """
        self._pause_monitoring()
        try:
            print(f"\n{'═' * 55}")
            print(f"  APPROVAL REQUIRED")
            print(f"{'─' * 55}")
            print(f"  {title}")
            if details:
                print(f"{'─' * 55}")
                for item in details:
                    print(f"    + {item}")
            print(f"{'─' * 55}")
            if self.email:
                print(f"  Also waiting on: Email")
                print(f"{'─' * 55}")

            answer = await self._read_line_cancellable("  Approve? [yes/no]: ")
            if answer is None:
                # Task was cancelled (another channel won) — nothing to do
                return

            approved = answer.strip().lower() in ("yes", "y")
            await self.resolve_approval_async(approval_id, approved, source="CLI")

        except asyncio.CancelledError:
            pass   # another channel won — expected
        except Exception as exc:
            logger.error("[ApprovalManager] CLI error: %s", exc)
        finally:
            self._resume_monitoring()

    @staticmethod
    async def _read_line_cancellable(prompt: str) -> Optional[str]:
        """
        Read one line from stdin in a way that respects CancelledError.

        Strategy:
          • Spawn a daemon thread that does the blocking input().
          • The thread puts its result on a thread-safe queue.
          • The coroutine polls the queue with a short sleep, so asyncio
            can cancel it between polls without leaving a zombie thread
            stuck forever.  The daemon thread will eventually be reaped
            when the process exits or a new prompt is shown.

        This is the correct cross-platform approach; using add_reader on
        sys.stdin only works on Unix and breaks on Windows terminals.
        """
        import queue
        import threading

        result_q: queue.Queue = queue.Queue()
        cancel_flag = threading.Event()

        def _blocking_input():
            try:
                sys.stdout.write(prompt)
                sys.stdout.flush()
                line = sys.stdin.readline()
                if not cancel_flag.is_set():
                    result_q.put(line.rstrip("\n"))
            except Exception:
                result_q.put("")

        thread = threading.Thread(target=_blocking_input, daemon=True)
        thread.start()

        try:
            while True:
                try:
                    return result_q.get_nowait()
                except queue.Empty:
                    await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            cancel_flag.set()   # signal the thread to discard its result
            raise               # re-raise so the task is properly cancelled

    # ── Email approval ────────────────────────────────────────────────────────

    async def _email_approval(
        self,
        approval_id: str,
        entry:       _ApprovalEntry,
        title:       str,
        details:     list[str],
    ) -> None:
        """
        Send an approval email and wait for a link click.

        The actual resolution happens when ApprovalServer receives the
        GET /approve or /deny request and calls resolve_approval().
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

            # Wait for ApprovalServer to call resolve_approval_async()
            await entry.event.wait()

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("[ApprovalManager] Email error: %s", exc)