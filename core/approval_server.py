"""
core/approval_server.py
────────────────────────
Lightweight aiohttp server that lives inside the SDK's asyncio event loop.

Handles one kind of inbound request:

  GET /approve?id=<approval_id>
  GET /deny?id=<approval_id>
      Email clients open these links when the engineer clicks the
      Approve / Deny buttons in the approval email.

Both routes call ApprovalManager.resolve_approval_async() to unblock the
asyncio.Event that request_approval() is waiting on.

Ngrok tunnel (optional):
  If NGROK_AUTHTOKEN is set in .env, the server automatically opens a
  public tunnel and sets APPROVAL_BASE_URL on the EmailClient so email
  links work from outside localhost.

  If ngrok is not configured, email approvals only work when the engineer
  is on the same machine as the running SDK. CLI approvals always work.

BUG FIX (v2):
  The original code called email_client.send_approval_request() before
  start() had finished discovering the public URL (the ngrok handshake
  is async and takes ~1–2 s). Gate 1's email was sent with an empty
  approval_base_url, producing broken links like "/approve?id=…".

  Fix: start() now resolves the public URL BEFORE returning.  The
  Orchestrator must await server.start() before kicking off any approval
  gate — which it already does — so no orchestrator changes are needed.

  Additionally, route handlers now call resolve_approval_async() (the
  proper async-safe method) instead of the sync shim, ensuring the
  asyncio.Lock inside _ApprovalEntry is respected.

Usage (managed by Orchestrator):
    server = ApprovalServer(approval_manager, email_client)
    await server.start()          # call once before the pipeline begins
    ...
    await server.stop()           # call once after the pipeline ends
    public_url = server.public_url  # already set on EmailClient.approval_base_url
"""

import asyncio
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

try:
    from aiohttp import web as _web
    _AIOHTTP_AVAILABLE = True
except ImportError:
    _AIOHTTP_AVAILABLE = False
    logger.warning("[ApprovalServer] aiohttp not installed — Email approvals disabled")


class ApprovalServer:
    """
    Async HTTP server for receiving email link clicks (approve/deny).

    Lifecycle:
        await server.start()   → binds port, resolves public URL, injects
                                 it into EmailClient BEFORE returning
        await server.stop()    → closes tunnel, stops server
    """

    def __init__(
        self,
        approval_manager,               # core.approval_manager.ApprovalManager
        email_client=None,              # core.email_client.EmailClient  (optional)
        host:    str = "0.0.0.0",
        port:    int = 0,               # 0 = OS picks a free port
    ):
        self._approval_manager = approval_manager
        self._email            = email_client
        self._host             = host
        self._port             = port

        self._runner:     Optional[object] = None
        self._site:       Optional[object] = None
        self._ngrok_tunnel                 = None
        self.public_url:  str              = ""
        self.local_url:   str              = ""

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> str:
        """
        Start the HTTP server and (optionally) open a ngrok tunnel.

        IMPORTANT: This method does NOT return until the public URL is
        fully resolved (ngrok handshake complete or fallback decided).
        The EmailClient's approval_base_url is set before returning so
        the very first approval email contains valid links.

        Returns the public_url.
        """
        if not _AIOHTTP_AVAILABLE:
            logger.warning("[ApprovalServer] aiohttp missing — server not started")
            return ""

        app = _web.Application()
        app.router.add_get("/approve", self._handle_email_approve)
        app.router.add_get("/deny",    self._handle_email_deny)
        app.router.add_get("/health",  self._handle_health)

        self._runner = _web.AppRunner(app, access_log=None)
        await self._runner.setup()

        self._site = _web.TCPSite(self._runner, self._host, self._port)
        await self._site.start()

        # Discover the actual port chosen by the OS
        bound_port = self._site._server.sockets[0].getsockname()[1]
        self.local_url = f"http://localhost:{bound_port}"
        logger.info("[ApprovalServer] listening on %s", self.local_url)

        # ── Resolve public URL before returning ────────────────────────
        # _try_ngrok is awaited here (not fire-and-forget) so that
        # self.public_url is set BEFORE the caller sends any emails.
        self.public_url = await self._try_ngrok(bound_port)
        if not self.public_url:
            self.public_url = self.local_url
            logger.info(
                "[ApprovalServer] ngrok not configured — email links will use %s "
                "(only works if engineer is on same machine)",
                self.local_url,
            )

        # Inject the resolved base URL into the email client
        if self._email:
            self._email.approval_base_url = self.public_url
            logger.info(
                "[ApprovalServer] EmailClient.approval_base_url = %s",
                self.public_url,
            )

        print(f"\n  [ApprovalServer] Listening at: {self.public_url}")
        print(f"  [ApprovalServer] Email approve/deny links will use this base URL.\n")

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

        FIX: now calls resolve_approval_async() (the proper async-safe
        method with the asyncio.Lock) instead of the synchronous shim.
        """
        approval_id = request.rel_url.query.get("id", "")
        if not approval_id:
            return _web.Response(
                content_type="text/html",
                text=self._html_page(
                    "❌ Invalid link",
                    "No approval ID found in link.",
                    error=True,
                ),
            )

        # Use the async-safe version so the Lock inside _ApprovalEntry is honoured
        resolved = await self._approval_manager.resolve_approval_async(
            approval_id=approval_id,
            approved=approved,
            source="Email",
        )

        if resolved is None:
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
        Fully awaited — returns only after the tunnel URL is confirmed.
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
        color: str  = "#2c3e50",
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