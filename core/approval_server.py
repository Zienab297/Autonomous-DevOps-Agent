"""
core/approval_server.py
────────────────────────
Lightweight aiohttp server that lives inside the SDK's asyncio event loop.

Routes:
  GET /approve?id=<approval_id>  — engineer clicked Approve in email
  GET /deny?id=<approval_id>     — engineer clicked Deny in email
  GET /health                    — health check

Both approval routes call ApprovalManager.resolve_approval() to unblock
the asyncio.Event that request_approval() is waiting on.

Public tunnel — Cloudflare:
  Uses `cloudflared tunnel --url http://localhost:PORT` to open a public
  HTTPS tunnel with no account or token required.

  Setup (one-time, Windows):
    1. Download https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe
    2. Rename to cloudflared.exe and move to C:\\Windows\\System32\\
    3. Verify: cloudflared --version

  On Linux/macOS: install via package manager or download the binary.

  If cloudflared is not found, the server falls back to localhost-only
  (email approve/deny links only work on the same machine).

Usage (managed by Orchestrator):
    server = ApprovalServer(approval_manager, email_client)
    await server.start()   # binds port, opens tunnel, sets email base URL
    ...
    await server.stop()    # kills tunnel process, stops server
"""

import asyncio
import logging
import subprocess
import re
from typing import Optional

logger = logging.getLogger(__name__)

try:
    from aiohttp import web as _web
    _AIOHTTP_AVAILABLE = True
except ImportError:
    _AIOHTTP_AVAILABLE = False
    logger.warning("[ApprovalServer] aiohttp not installed — email approvals disabled")


class ApprovalServer:

    def __init__(
        self,
        approval_manager,
        email_client=None,
        host: str = "0.0.0.0",
        port: int = 0,          # 0 = OS picks a free port
    ):
        self._approval_manager = approval_manager
        self._email            = email_client
        self._host             = host
        self._port             = port

        self._runner:    Optional[object]            = None
        self._site:      Optional[object]            = None
        self._cf_proc:   Optional[subprocess.Popen] = None  # cloudflared process
        self.public_url: str                         = ""
        self.local_url:  str                         = ""

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> str:
        """
        Bind the HTTP server and open a Cloudflare tunnel.
        Returns the public URL set on EmailClient.approval_base_url.
        """
        if not _AIOHTTP_AVAILABLE:
            logger.warning("[ApprovalServer] aiohttp missing — server not started")
            return ""

        app = _web.Application()
        app.router.add_get("/approve", self._handle_approve)
        app.router.add_get("/deny",    self._handle_deny)
        app.router.add_get("/health",  self._handle_health)

        self._runner = _web.AppRunner(app, access_log=None)
        await self._runner.setup()

        self._site = _web.TCPSite(self._runner, self._host, self._port)
        await self._site.start()

        bound_port     = self._site._server.sockets[0].getsockname()[1]
        self.local_url = f"http://localhost:{bound_port}"
        logger.info("[ApprovalServer] listening on %s", self.local_url)

        self.public_url = await self._start_cloudflare(bound_port)
        if not self.public_url:
            self.public_url = self.local_url
            logger.info(
                "[ApprovalServer] cloudflared not found — email links use %s "
                "(only works on this machine)", self.local_url
            )

        if self._email:
            self._email.approval_base_url = self.public_url

        return self.public_url

    async def stop(self) -> None:
        """Terminate the cloudflared tunnel and shut down the HTTP server."""
        if self._cf_proc:
            try:
                self._cf_proc.terminate()
                self._cf_proc.wait(timeout=5)
            except Exception:
                pass
            self._cf_proc = None

        if self._runner:
            await self._runner.cleanup()
            self._runner = None

        logger.info("[ApprovalServer] stopped")

    # ── Cloudflare tunnel ─────────────────────────────────────────────────────

    async def _start_cloudflare(self, port: int) -> str:
        """
        Launch `cloudflared tunnel --url http://localhost:PORT` as a subprocess,
        parse the public trycloudflare.com URL from its stderr output, and
        return it.  Returns '' if cloudflared is not found or URL not parsed.
        """
        try:
            proc = await asyncio.to_thread(
                self._launch_cloudflared, port
            )
            if proc is None:
                return ""
            self._cf_proc = proc

            # cloudflared prints the URL to stderr; wait up to 15s for it
            url = await asyncio.wait_for(
                asyncio.to_thread(self._read_cloudflare_url, proc),
                timeout=20,
            )
            if url:
                logger.info("[ApprovalServer] Cloudflare tunnel: %s", url)
                print(f"\n  [ApprovalServer] 🌐 Public URL: {url}\n")
            return url or ""

        except asyncio.TimeoutError:
            logger.warning("[ApprovalServer] cloudflared URL not found within 20s")
            return ""
        except Exception as exc:
            logger.warning("[ApprovalServer] cloudflared failed: %s", exc)
            return ""

    @staticmethod
    def _launch_cloudflared(port: int) -> Optional[subprocess.Popen]:
        """Spawn cloudflared as a subprocess. Returns None if not found."""
        try:
            proc = subprocess.Popen(
                ["cloudflared", "tunnel", "--url", f"http://localhost:{port}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            return proc
        except FileNotFoundError:
            logger.warning(
                "[ApprovalServer] cloudflared not found — install it and add to PATH. "
                "See: https://github.com/cloudflare/cloudflared/releases/latest"
            )
            return None

    @staticmethod
    def _read_cloudflare_url(proc: subprocess.Popen) -> str:
        """
        Read cloudflared's stderr line by line until we find the public URL.
        cloudflared prints lines like:
          INF | Your quick Tunnel has been created! Visit it at (it may take some time to be reachable): https://xxxx.trycloudflare.com
        or just:
          https://xxxx.trycloudflare.com
        """
        pattern = re.compile(r"https://[a-z0-9\-]+\.trycloudflare\.com")
        for line in proc.stderr:  # type: ignore[union-attr]
            line = line.strip()
            if not line:
                continue
            match = pattern.search(line)
            if match:
                return match.group(0)
        return ""

    # ── Route handlers ────────────────────────────────────────────────────────

    async def _handle_approve(self, request: "_web.Request") -> "_web.Response":
        return await self._handle_click(request, approved=True)

    async def _handle_deny(self, request: "_web.Request") -> "_web.Response":
        return await self._handle_click(request, approved=False)

    async def _handle_click(
        self, request: "_web.Request", approved: bool
    ) -> "_web.Response":
        approval_id = request.rel_url.query.get("id", "")
        if not approval_id:
            return _web.Response(
                content_type="text/html",
                text=self._page("❌ Invalid link", "No approval ID in link.", error=True),
            )

        resolved = self._approval_manager.resolve_approval(
            approval_id=approval_id,
            approved=approved,
            source="Email",
        )

        if resolved is None:
            label = "Approved" if approved else "Denied"
            return _web.Response(
                content_type="text/html",
                text=self._page(
                    "⏩ Already resolved",
                    f"Another channel already resolved this. Your intent: {label}.",
                ),
            )

        label = "✅ Approved" if approved else "❌ Denied"
        color = "#28a745"     if approved else "#dc3545"
        return _web.Response(
            content_type="text/html",
            text=self._page(label, "Decision recorded. You can close this tab.", color=color),
        )

    async def _handle_health(self, request: "_web.Request") -> "_web.Response":
        return _web.Response(text="ok")

    # ── HTML page builder ─────────────────────────────────────────────────────

    @staticmethod
    def _page(title: str, body: str, color: str = "#2c3e50", error: bool = False) -> str:
        if error:
            color = "#c0392b"
        return f"""<!DOCTYPE html>
<html>
<head><title>DevOps Agent</title></head>
<body style="font-family:Arial,sans-serif;background:#f5f5f5;display:flex;
             align-items:center;justify-content:center;min-height:100vh;margin:0;">
  <div style="background:#fff;border-radius:10px;padding:40px 60px;
              box-shadow:0 4px 16px rgba(0,0,0,.12);text-align:center;max-width:480px;">
    <h1 style="color:{color};font-size:2em;margin-bottom:12px;">{title}</h1>
    <p style="color:#555;font-size:1.1em;">{body}</p>
    <p style="color:#aaa;font-size:.85em;margin-top:24px;">DevOps Agent</p>
  </div>
</body>
</html>"""