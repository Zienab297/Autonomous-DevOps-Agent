"""
core/email_client.py
────────────────────
Async email client for the DevOps SDK.

Two routing lanes:
  Approval emails    → ALERT_ENGINEER_EMAIL only (one person, one click).
  Normal alerts      → ALERT_ENGINEER_EMAIL only (LOW / MEDIUM severity).
  Emergency alerts   → ALERT_ENGINEER_EMAIL + all ALERT_TEAM_EMAILS
                       concurrently (HIGH / CRITICAL severity).

Configuration (.env):
    ALERT_SMTP_HOST         smtp.gmail.com
    ALERT_SMTP_PORT         587
    ALERT_SMTP_USERNAME     you@gmail.com
    ALERT_SMTP_PASSWORD     <16-char Gmail app password>
    ALERT_FROM_ADDRESS      DevOps Agent <you@gmail.com>
    ALERT_ENGINEER_EMAIL    lead@company.com
    ALERT_TEAM_EMAILS       a@co.com,b@co.com,c@co.com   (comma-separated)
    ALERT_APPROVAL_BASE_URL https://xxxx.trycloudflare.com
"""

import asyncio
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text      import MIMEText
from datetime             import datetime
from typing               import List, Optional

logger = logging.getLogger(__name__)


class EmailClient:

    def __init__(
        self,
        smtp_host:         str,
        smtp_port:         int,
        username:          str,
        password:          str,
        from_address:      str,
        engineer_email:    str,
        team_emails:       Optional[List[str]] = None,
        approval_base_url: str  = "",
        use_tls:           bool = True,
    ):
        self.smtp_host         = smtp_host
        self.smtp_port         = smtp_port
        self.username          = username
        self.password          = password
        self.from_address      = from_address
        self.engineer_email    = engineer_email
        self.team_emails       = team_emails or []
        self.approval_base_url = approval_base_url.rstrip("/")
        self.use_tls           = use_tls

    # ── Approval emails ───────────────────────────────────────────────────────

    async def send_approval_request(
        self,
        approval_id: str,
        title:       str,
        details:     List[str],
    ) -> None:
        """
        Send HTML approval email with Approve / Deny buttons to the engineer only.
        The links hit the ApprovalServer which resolves the asyncio.Event.
        """
        base         = self.approval_base_url
        approve_url  = f"{base}/approve?id={approval_id}"
        deny_url     = f"{base}/deny?id={approval_id}"
        details_html = "".join(f"<li>{d}</li>" for d in details)

        html = f"""<!DOCTYPE html>
<html>
<body style="font-family:Arial,sans-serif;background:#f5f5f5;padding:20px;">
  <div style="max-width:600px;margin:0 auto;background:#fff;
              border-radius:8px;padding:30px;box-shadow:0 2px 8px rgba(0,0,0,.1);">
    <h2 style="color:#1a1a2e;margin-top:0;">🔐 Approval Required</h2>
    <h3 style="color:#333;">{title}</h3>
    <ul style="color:#555;line-height:1.8;">{details_html}</ul>
    <p style="color:#888;font-size:12px;">ID: <code>{approval_id}</code></p>
    <div style="text-align:center;margin:30px 0;">
      <a href="{approve_url}"
         style="background:#28a745;color:#fff;padding:14px 36px;border-radius:6px;
                text-decoration:none;font-size:16px;font-weight:bold;margin-right:12px;">
        ✅ Approve</a>
      <a href="{deny_url}"
         style="background:#dc3545;color:#fff;padding:14px 36px;border-radius:6px;
                text-decoration:none;font-size:16px;font-weight:bold;">
        ❌ Deny</a>
    </div>
    <p style="color:#aaa;font-size:11px;text-align:center;">
      Expires in 5 minutes. Only the first click counts.
    </p>
  </div>
</body>
</html>"""

        await self._send(
            recipients=[self.engineer_email],
            subject=f"[Approval Required] {title}",
            html=html,
            high_priority=True,
        )

    # ── Alert emails ──────────────────────────────────────────────────────────

    async def send_alert(
        self,
        title:   str,
        message: str,
        urgent:  bool = False,
    ) -> None:
        """
        Send a one-way alert.

        urgent=False → engineer only (LOW / MEDIUM).
        urgent=True  → engineer + full team concurrently (HIGH / CRITICAL).
                       Each To: shows only that recipient's address.
        """
        prefix  = "🚨 URGENT" if urgent else "📢 Notice"
        subject = f"[DevOps Agent] {prefix}: {title}"

        team_banner = (
            '<p style="background:#fff3cd;border:1px solid #ffc107;padding:10px 16px;'
            'border-radius:4px;color:#856404;font-weight:bold;font-size:13px;">'
            '⚠️ HIGH / CRITICAL — full team notified</p>'
            if urgent else ""
        )

        html = f"""<!DOCTYPE html>
<html>
<body style="font-family:Arial,sans-serif;background:#f5f5f5;padding:20px;">
  <div style="max-width:600px;margin:0 auto;background:#fff;
              border-radius:8px;padding:30px;box-shadow:0 2px 8px rgba(0,0,0,.1);">
    <h2 style="color:{'#c0392b' if urgent else '#2c3e50'};margin-top:0;">
      {'🚨 ' if urgent else '📢 '}{title}</h2>
    {team_banner}
    <p style="color:#555;line-height:1.7;white-space:pre-wrap;">{message}</p>
    <p style="color:#aaa;font-size:11px;margin-top:30px;">
      DevOps Agent · {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}
    </p>
  </div>
</body>
</html>"""

        recipients = [self.engineer_email]
        if urgent and self.team_emails:
            for addr in self.team_emails:
                if addr and addr != self.engineer_email:
                    recipients.append(addr)
            logger.info("[EmailClient] Emergency broadcast to %d recipients", len(recipients))

        await asyncio.gather(
            *[self._send([addr], subject, html, urgent) for addr in recipients],
            return_exceptions=True,
        )

    # ── SMTP helpers ──────────────────────────────────────────────────────────

    async def _send(
        self,
        recipients:    List[str],
        subject:       str,
        html:          str,
        high_priority: bool = False,
    ) -> None:
        await asyncio.to_thread(
            self._send_sync, recipients, subject, html, high_priority
        )

    def _send_sync(
        self,
        recipients:    List[str],
        subject:       str,
        html:          str,
        high_priority: bool = False,
    ) -> None:
        for to_addr in recipients:
            msg = MIMEMultipart("alternative")
            msg["From"]    = self.from_address
            msg["To"]      = to_addr
            msg["Subject"] = subject
            if high_priority:
                msg["X-Priority"] = "1"
                msg["Importance"] = "high"
            msg.attach(MIMEText(html, "html"))
            try:
                with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=15) as s:
                    if self.use_tls:
                        s.starttls()
                    s.login(self.username, self.password)
                    s.sendmail(self.from_address, to_addr, msg.as_string())
                logger.info("[EmailClient] sent: %s → %s", subject, to_addr)
            except Exception as exc:
                logger.error("[EmailClient] SMTP error (%s): %s", to_addr, exc)