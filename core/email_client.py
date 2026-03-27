"""
core/email_client.py
────────────────────
Async Email client for the DevOps SDK.

Handles two concerns:
  1. Approval emails  — HTML email with ✅ Approve and ❌ Deny buttons/links
                        that point to the ApprovalServer's HTTP endpoints.
  2. Alert emails     — One-way plain-text notifications.

Adapted from agents/alert_agent/notifications/email.py but:
  • Uses asyncio.to_thread() for SMTP (blocking) — non-blocking in event loop.
  • Sends styled HTML emails with big clickable buttons for approvals.
  • approval_base_url comes from .env (ngrok URL or real domain).

Configuration (.env):
    SMTP_HOST              smtp.gmail.com
    SMTP_PORT              587
    SMTP_USERNAME          you@gmail.com
    SMTP_PASSWORD          <app-password>
    EMAIL_FROM             DevOps Agent <you@gmail.com>
    EMAIL_TO               engineer@company.com
    APPROVAL_BASE_URL      https://xxxx.ngrok.io   (or http://localhost:PORT)
"""

import asyncio
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text      import MIMEText
from datetime             import datetime

logger = logging.getLogger(__name__)


class EmailClient:
    """
    Async email wrapper.

    send_approval_request() builds an HTML email with big Approve / Deny
    buttons — the links hit the ApprovalServer which resolves the asyncio.Event.

    All public methods are async and safe to await inside the SDK's event loop.
    """

    def __init__(
        self,
        smtp_host:        str,
        smtp_port:        int,
        username:         str,
        password:         str,
        from_address:     str,
        to_address:       str,
        approval_base_url: str = "",   # e.g. https://xxxx.ngrok.io
        use_tls:          bool = True,
    ):
        self.smtp_host         = smtp_host
        self.smtp_port         = smtp_port
        self.username          = username
        self.password          = password
        self.from_address      = from_address
        self.to_address        = to_address
        self.approval_base_url = approval_base_url.rstrip("/")
        self.use_tls           = use_tls

    # ── Approval flow ─────────────────────────────────────────────────────────

    async def send_approval_request(
        self,
        approval_id: str,
        title:       str,
        details:     list[str],
    ) -> None:
        """
        Send an HTML approval email with Approve / Deny buttons.

        The buttons are anchor tags linking to:
          {approval_base_url}/approve?id={approval_id}
          {approval_base_url}/deny?id={approval_id}

        The ApprovalServer handles these GET requests and resolves the
        asyncio.Event that ApprovalManager is waiting on.
        """
        base         = self.approval_base_url
        approve_url  = f"{base}/approve?id={approval_id}"
        deny_url     = f"{base}/deny?id={approval_id}"
        details_html = "".join(f"<li>{d}</li>" for d in details)

        subject = f"[Approval Required] {title}"
        html    = f"""
<!DOCTYPE html>
<html>
<body style="font-family:Arial,sans-serif;background:#f5f5f5;padding:20px;">
  <div style="max-width:600px;margin:0 auto;background:#fff;
              border-radius:8px;padding:30px;box-shadow:0 2px 8px rgba(0,0,0,.1);">

    <h2 style="color:#1a1a2e;margin-top:0;">🔐 Approval Required</h2>
    <h3 style="color:#333;">{title}</h3>

    <ul style="color:#555;line-height:1.8;">
      {details_html}
    </ul>

    <p style="color:#888;font-size:12px;">Approval ID: <code>{approval_id}</code></p>

    <div style="text-align:center;margin:30px 0;">
      <a href="{approve_url}"
         style="background:#28a745;color:#fff;padding:14px 36px;
                border-radius:6px;text-decoration:none;font-size:16px;
                font-weight:bold;margin-right:12px;">
        ✅ Approve
      </a>
      <a href="{deny_url}"
         style="background:#dc3545;color:#fff;padding:14px 36px;
                border-radius:6px;text-decoration:none;font-size:16px;
                font-weight:bold;">
        ❌ Deny
      </a>
    </div>

    <p style="color:#aaa;font-size:11px;text-align:center;">
      This link expires in 5 minutes. Only the first click counts.
    </p>
  </div>
</body>
</html>
"""
        await self._send(subject=subject, html=html, high_priority=True)

    # ── Alert / notification flow ─────────────────────────────────────────────

    async def send_alert(
        self,
        title:   str,
        message: str,
        urgent:  bool = False,
    ) -> None:
        """Send a one-way alert notification email."""
        prefix  = "🚨 URGENT" if urgent else "📢 Notice"
        subject = f"[DevOps Agent] {prefix}: {title}"
        html    = f"""
<!DOCTYPE html>
<html>
<body style="font-family:Arial,sans-serif;background:#f5f5f5;padding:20px;">
  <div style="max-width:600px;margin:0 auto;background:#fff;
              border-radius:8px;padding:30px;box-shadow:0 2px 8px rgba(0,0,0,.1);">

    <h2 style="color:{'#c0392b' if urgent else '#2c3e50'};margin-top:0;">
      {'🚨 ' if urgent else '📢 '}{title}
    </h2>

    <p style="color:#555;line-height:1.7;white-space:pre-wrap;">{message}</p>

    <p style="color:#aaa;font-size:11px;margin-top:30px;">
      Sent by DevOps Agent at {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}
    </p>
  </div>
</body>
</html>
"""
        await self._send(subject=subject, html=html, high_priority=urgent)

    # ── SMTP helper ───────────────────────────────────────────────────────────

    async def _send(
        self,
        subject:       str,
        html:          str,
        high_priority: bool = False,
    ) -> None:
        """Dispatch SMTP send in a thread so it doesn't block the event loop."""
        await asyncio.to_thread(
            self._send_sync,
            subject=subject,
            html=html,
            high_priority=high_priority,
        )

    def _send_sync(
        self,
        subject:       str,
        html:          str,
        high_priority: bool = False,
    ) -> None:
        """Blocking SMTP send — called via asyncio.to_thread()."""
        msg = MIMEMultipart("alternative")
        msg["From"]    = self.from_address
        msg["To"]      = self.to_address
        msg["Subject"] = subject

        if high_priority:
            msg["X-Priority"] = "1"
            msg["Importance"] = "high"

        msg.attach(MIMEText(html, "html"))

        try:
            with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=15) as server:
                if self.use_tls:
                    server.starttls()
                server.login(self.username, self.password)
                server.sendmail(self.from_address, self.to_address, msg.as_string())
            logger.info("[EmailClient] sent: %s", subject)
        except smtplib.SMTPException as exc:
            logger.error("[EmailClient] SMTP error: %s", exc)
        except Exception as exc:
            logger.error("[EmailClient] unexpected error: %s", exc)