"""
core/approval_server.py
────────────────────────
Lightweight aiohttp server that lives inside the SDK's asyncio event loop.

Handles two kinds of inbound requests:

  1. POST /slack/interactive
       Slack sends this when an engineer clicks ✅ Approve or ❌ Deny
       on a Block Kit message.  Payload is form-encoded JSON identical
       to the format handled by alert_agent/main/slack_webhook.py.

  2. GET /approve?id=<approval_id>
     GET /deny?id=<approval_id>
       Email clients open these links when the engineer clicks the
       Approve / Deny buttons in the approval email.

Both routes call ApprovalManager.resolve_approval() to unblock the
asyncio.Event that request_approval() is waiting on.

Ngrok tunnel (optional):
  If NGROK_AUTHTOKEN is set in .env, the server automatically opens a
  public tunnel and sets APPROVAL_BASE_URL on the EmailClient so email
  links work from outside localhost.

  If ngrok is not configured, only Slack (socket-less webhook) and CLI
  approvals work for remote engineers.  Email approvals fall back to
  CLI-only gracefully.

Usage (managed by Orchestrator):
    server = ApprovalServer(approval_manager, slack_client, email_client)
    await server.start()          # call once before the pipeline begins
    ...
    await server.stop()           # call once after the pipeline ends
    public_url = server.public_url  # set on EmailClient.approval_base_url
"""

import asyncio
import json
import logging
import os
import urllib.parse
from typing import Optional

logger = logging.getLogger(__name__)

# aiohttp is a lightweight async web framework — already used by devops pipeline
try:
    from aiohttp import web as _web
    _AIOHTTP_AVAILABLE = True
except ImportError:
    _AIOHTTP_AVAILABLE = False
    logger.warning("[ApprovalServer] aiohttp not installed — Slack/Email approvals disabled")


class ApprovalServer:
    """
    Async HTTP server for receiving Slack button clicks and email link clicks.

    Lifecycle:
        await server.start()   → binds port, optionally opens ngrok tunnel
        await server.stop()    → closes tunnel, stops server
    """

    def __init__(
        self,
        approval_manager,               # core.approval_manager.ApprovalManager
        slack_client=None,              # core.slack_client.SlackClient  (optional)
        email_client=None,              # core.email_client.EmailClient  (optional)
        host:    str = "0.0.0.0",
        port:    int = 0,               # 0 = OS picks a free port
    ):
        self._approval_manager = approval_manager
        self._slack            = slack_client
        self._email            = email_client
        self._host             = host
        self._port             = port

        self._runner:     Optional[object] = None   # aiohttp AppRunner
        self._site:       Optional[object] = None   # aiohttp TCPSite
        self._ngrok_tunnel                 = None
        self.public_url:  str              = ""     # set after start()
        self.local_url:   str              = ""     # set after start()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> str:
        """
        Start the HTTP server and (optionally) open a ngrok tunnel.
        Returns the public_url that should be set on EmailClient.approval_base_url.
        """
        if not _AIOHTTP_AVAILABLE:
            logger.warning("[ApprovalServer] aiohttp missing — server not started")
            return ""

        app = _web.Application()
        app.router.add_post("/slack/interactive", self._handle_slack)
        app.router.add_get("/approve",            self._handle_email_approve)
        app.router.add_get("/deny",               self._handle_email_deny)
        app.router.add_get("/health",             self._handle_health)

        self._runner = _web.AppRunner(app, access_log=None)
        await self._runner.setup()

        self._site = _web.TCPSite(self._runner, self._host, self._port)
        await self._site.start()

        # Discover the actual port chosen by the OS
        bound_port = self._site._server.sockets[0].getsockname()[1]
        self.local_url = f"http://localhost:{bound_port}"

        logger.info("[ApprovalServer] listening on %s", self.local_url)

        # Try to open ngrok tunnel
        self.public_url = await self._try_ngrok(bound_port)
        if not self.public_url:
            self.public_url = self.local_url
            logger.info(
                "[ApprovalServer] ngrok not configured — email links will use %s "
                "(only works if engineer is on same machine)",
                self.local_url,
            )

        # Inject the base URL into the email client so links are correct
        if self._email:
            self._email.approval_base_url = self.public_url

        return self.public_url

    async def stop(self) -> None:
        """Shut down the HTTP server and close any ngrok tunnel."""
        if self._ngrok_tunnel:
            try:
                from pyngrok import ngrok as _ngrok
                _ngrok.disconnect(self._ngrok_tunnel.public_url)
            except Exception:
                pass
            self._ngrok_tunnel = None

        if self._runner:
            await self._runner.cleanup()
            self._runner = None

        logger.info("[ApprovalServer] stopped")

    # ── Route handlers ────────────────────────────────────────────────────────

    async def _handle_slack(self, request: "_web.Request") -> "_web.Response":
        """
        POST /slack/interactive

        Slack sends form-encoded: payload=<url-encoded JSON>
        We parse it, extract action_id + approval_id, and resolve.
        """
        try:
            body    = await request.text()
            decoded = urllib.parse.unquote_plus(body)
            if not decoded.startswith("payload="):
                return _web.Response(status=400, text="bad request")

            payload       = json.loads(decoded[len("payload="):])
            actions       = payload.get("actions", [])
            user_id       = payload.get("user", {}).get("id", "slack-user")
            message_ts    = payload.get("message", {}).get("ts")

            if not actions:
                return _web.Response(status=200)

            action      = actions[0]
            action_id   = action.get("action_id", "")
            value       = json.loads(action.get("value", "{}"))
            approval_id = value.get("approval_id")

            if not approval_id:
                logger.warning("[ApprovalServer] Slack payload missing approval_id")
                return _web.Response(status=200)

            approved = action_id == "devops_approve"

            # Resolve the approval
            resolved = self._approval_manager.resolve_approval(
                approval_id=approval_id,
                approved=approved,
                source=f"Slack ({user_id})",
            )

            # Update the Slack message to show the decision
            if resolved and self._slack and message_ts:
                asyncio.create_task(
                    self._slack.update_approval_message(
                        ts=message_ts,
                        approved=approved,
                        resolved_by=user_id,
                    )
                )

            logger.info(
                "[ApprovalServer] Slack: approval_id=%s action=%s user=%s",
                approval_id, action_id, user_id,
            )
            # Slack requires a 200 with empty body within 3 seconds
            return _web.Response(status=200)

        except Exception as exc:
            logger.error("[ApprovalServer] Slack handler error: %s", exc)
            return _web.Response(status=200)   # always 200 to Slack

    async def _handle_email_approve(self, request: "_web.Request") -> "_web.Response":
        return await self._handle_email_click(request, approved=True)

    async def _handle_email_deny(self, request: "_web.Request") -> "_web.Response":
        return await self._handle_email_click(request, approved=False)

    async def _handle_email_click(
        self, request: "_web.Request", approved: bool
    ) -> "_web.Response":
        """
        GET /approve?id=<approval_id>
        GET /deny?id=<approval_id>

        Called when engineer clicks an email button.
        Shows a simple HTML confirmation page.
        """
        approval_id = request.rel_url.query.get("id", "")
        if not approval_id:
            return _web.Response(
                content_type="text/html",
                text=self._html_page("❌ Invalid link", "No approval ID found in link.", error=True),
            )

        resolved = self._approval_manager.resolve_approval(
            approval_id=approval_id,
            approved=approved,
            source="Email",
        )

        if resolved is None:
            # Already resolved by another channel (CLI or Slack won the race)
            label = "Approved" if approved else "Denied"
            return _web.Response(
                content_type="text/html",
                text=self._html_page(
                    "⏩ Already Resolved",
                    f"This approval was already resolved by another channel. "
                    f"Your intended decision was: {label}.",
                ),
            )

        label = "✅ Approved" if approved else "❌ Denied"
        color = "#28a745"     if approved else "#dc3545"
        return _web.Response(
            content_type="text/html",
            text=self._html_page(
                label,
                "Your decision has been recorded. You can close this tab.",
                color=color,
            ),
        )

    async def _handle_health(self, request: "_web.Request") -> "_web.Response":
        return _web.Response(text="ok")

    # ── Ngrok ─────────────────────────────────────────────────────────────────

    async def _try_ngrok(self, port: int) -> str:
        """
        Try to open a ngrok tunnel.
        Returns the public https URL or '' if ngrok is not configured.
        """
        token = os.getenv("NGROK_AUTHTOKEN", "")
        if not token:
            return ""
        try:
            from pyngrok import ngrok as _ngrok, conf as _conf
            _conf.get_default().auth_token = token
            tunnel = await asyncio.to_thread(_ngrok.connect, port)
            self._ngrok_tunnel = tunnel
            url = tunnel.public_url.replace("http://", "https://")
            logger.info("[ApprovalServer] ngrok tunnel: %s", url)
            print(f"\n  [ApprovalServer] 🌐 Public URL: {url}")
            return url
        except Exception as exc:
            logger.warning("[ApprovalServer] ngrok failed: %s", exc)
            return ""

    # ── HTML page builder ─────────────────────────────────────────────────────

    @staticmethod
    def _html_page(
        title: str,
        body:  str,
        color: str = "#2c3e50",
        error: bool = False,
    ) -> str:
        if error:
            color = "#c0392b"
        return f"""<!DOCTYPE html>
<html>
<head><title>DevOps Agent</title></head>
<body style="font-family:Arial,sans-serif;background:#f5f5f5;
             display:flex;align-items:center;justify-content:center;
             min-height:100vh;margin:0;">
  <div style="background:#fff;border-radius:10px;padding:40px 60px;
              box-shadow:0 4px 16px rgba(0,0,0,.12);text-align:center;
              max-width:480px;">
    <h1 style="color:{color};font-size:2em;margin-bottom:12px;">{title}</h1>
    <p style="color:#555;font-size:1.1em;">{body}</p>
    <p style="color:#aaa;font-size:.85em;margin-top:24px;">DevOps Agent</p>
  </div>
</body>
</html>"""