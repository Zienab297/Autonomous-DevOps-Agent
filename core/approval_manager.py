"""
core/approval_manager.py
-------------------------
Manages approvals across CLI, Slack, and Email.
First channel to respond wins — others are ignored.

KEY FIX: The MonitoringAgent prints a live dashboard on a background task.
This overwrites the CLI input prompt and the user can't type.
Solution: ApprovalManager accepts an optional `registry` reference.
Before showing the CLI prompt it calls monitoring_agent.pause_dashboard(),
and resumes it after the answer is received.

Usage:
    manager = ApprovalManager(slack, email, registry=orchestrator.registry)
    approved = await manager.request_approval(
        title="Scaffold complete — proceed to CI/CD?",
        details=["Dockerfile", "k8s/deployment.yaml", ...],
    )
    if approved:
        ...
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
        slack=None,
        email=None,
        timeout_seconds: int = 300,
        registry=None,          # ← AgentRegistry; used to pause monitoring dashboard
    ):
        self.slack           = slack
        self.email           = email
        self.timeout_seconds = timeout_seconds
        self.registry        = registry   # set via orchestrator after init

    # ── pause / resume monitoring dashboard ──────────────────────────────────

    def _pause_monitoring(self) -> None:
        """
        Stop the MonitoringAgent's live dashboard print loop so it doesn't
        overwrite the CLI approval prompt while the user is typing.
        """
        if not self.registry:
            return
        agent = self.registry.get_agent("monitoring_agent")
        if agent and hasattr(agent, "pause_dashboard"):
            try:
                agent.pause_dashboard()
            except Exception:
                pass

    def _resume_monitoring(self) -> None:
        """Resume the MonitoringAgent dashboard after the user has answered."""
        if not self.registry:
            return
        agent = self.registry.get_agent("monitoring_agent")
        if agent and hasattr(agent, "resume_dashboard"):
            try:
                agent.resume_dashboard()
            except Exception:
                pass

    # ── main entry point ──────────────────────────────────────────────────────

    async def request_approval(
        self,
        title  : str,
        details: list[str] = None,
        context: dict      = None,
    ) -> bool:
        """
        Request approval from all available channels simultaneously.
        Returns True (approved) or False (denied / timeout).
        """
        details = details or []
        context = context or {}

        logger.info(f"[ApprovalManager] Requesting approval: {title}")

        # Shared event — first responder sets it
        decision: dict = {"approved": None}
        resolved = asyncio.Event()

        async def resolve(approved: bool, source: str):
            if not resolved.is_set():
                decision["approved"] = approved
                decision["source"]   = source
                resolved.set()
                logger.info(
                    f"[ApprovalManager] Decision from {source}: "
                    f"{'APPROVED' if approved else 'DENIED'}"
                )

        # Build all tasks
        tasks = []

        # 1. CLI approval (always available)
        tasks.append(asyncio.create_task(
            self._cli_approval(title, details, resolve)
        ))

        # 2. Slack approval (if configured)
        if self.slack:
            tasks.append(asyncio.create_task(
                self._slack_approval(title, details, context, resolve)
            ))

        # 3. Email approval (if configured)
        if self.email:
            tasks.append(asyncio.create_task(
                self._email_approval(title, details, context, resolve)
            ))

        # Wait for first response or timeout
        try:
            await asyncio.wait_for(resolved.wait(), timeout=self.timeout_seconds)
        except asyncio.TimeoutError:
            logger.warning(f"[ApprovalManager] Approval timed out: {title}")
            decision["approved"] = False
            decision["source"]   = "timeout"

        # Cancel remaining tasks
        for task in tasks:
            if not task.done():
                task.cancel()

        approved = decision.get("approved", False)
        source   = decision.get("source", "unknown")
        print(
            f"\n  [Approval] Decision: "
            f"{'✅ APPROVED' if approved else '❌ DENIED'} (via {source})\n"
        )

        return approved

    # ── CLI approval ──────────────────────────────────────────────────────────

    async def _cli_approval(
        self,
        title  : str,
        details: list[str],
        resolve,
    ):
        """
        Ask for approval directly in the terminal.

        Pauses the MonitoringAgent dashboard before showing the prompt so
        background prints don't overwrite what the user is typing.
        Resumes the dashboard after the answer is received.
        """
        try:
            # ── pause monitoring dashboard ────────────────────────────────
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
                print(f"  Waiting for Slack / Email response too...")
                print(f"{'─' * 55}")

            # Run input in thread so it doesn't block the event loop
            answer = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: input("  Approve? [yes/no]: ").strip().lower()
            )

            approved = answer in ("yes", "y")
            await resolve(approved, "CLI")

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[ApprovalManager] CLI error: {e}")
        finally:
            # ── always resume monitoring dashboard ────────────────────────
            self._resume_monitoring()

    # ── Slack approval ────────────────────────────────────────────────────────

    async def _slack_approval(
        self,
        title  : str,
        details: list[str],
        context: dict,
        resolve,
    ):
        """Send approval request to Slack and wait for button click."""
        try:
            if not self.slack:
                return

            approval_id = await self.slack.send_approval_request(
                title=title,
                details=details,
                context=context,
            )

            result = await self.slack.wait_for_response(approval_id)
            await resolve(result, "Slack")

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[ApprovalManager] Slack error: {e}")

    # ── Email approval ────────────────────────────────────────────────────────

    async def _email_approval(
        self,
        title  : str,
        details: list[str],
        context: dict,
        resolve,
    ):
        """Send approval request via Email and wait for link click."""
        try:
            if not self.email:
                return

            approval_id = await self.email.send_approval_request(
                title=title,
                details=details,
                context=context,
            )

            result = await self.email.wait_for_response(approval_id)
            await resolve(result, "Email")

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[ApprovalManager] Email error: {e}")