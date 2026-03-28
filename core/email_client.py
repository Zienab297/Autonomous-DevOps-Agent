"""
core/email_client.py
────────────────────
Async Email client for the DevOps SDK.

Handles two concerns:
  1. Approval emails  — HTML email with ✅ Approve and ❌ Deny buttons/links
                        that point to the ApprovalServer's HTTP endpoints.
  2. Alert emails     — One-way plain-text notifications.
                        • Normal severity  → sent only to EMAIL_TO (lead dev).
                        • HIGH / CRITICAL  → emergency lane: sent to the full
                          team via EMAIL_TEAM (comma-separated list in .env).

Configuration (.env):
    SMTP_HOST              smtp.gmail.com
    SMTP_PORT              587
    SMTP_USERNAME          you@gmail.com
    SMTP_PASSWORD          <app-password>
    EMAIL_FROM             DevOps Agent <you@gmail.com>
    EMAIL_TO               lead-engineer@company.com
    EMAIL_TEAM             alice@company.com,bob@company.com,carol@company.com
    APPROVAL_BASE_URL      https://xxxx.ngrok.io   (or http://localhost:PORT)
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
    """
    Async email wrapper.

    send_approval_request() — HTML email with Approve / Deny buttons sent to
                              the primary developer (EMAIL_TO).

    send_alert()            — One-way notification.
                              urgent=True activates the emergency lane and
                              broadcasts to every address in team_addresses
                              in addition to the primary recipient.

    All public methods are async and safe to await inside the SDK's event loop.
    """

    def __init__(
        self,
        smtp_host:         str,
        smtp_port:         int,
        username:          str,
        password:          str,
        from_address:      str,
        to_address:        str,                     # primary developer
        team_addresses:    Optional[List[str]] = None,  # whole team (emergency lane)
        approval_base_url: str  = "",               # e.g. https://xxxx.ngrok.io
        use_tls:           bool = True,
    ):
        self.smtp_host         = smtp_host
        self.smtp_port         = smtp_port
        self.username          = username
        self.password          = password
        self.from_address      = from_address
        self.to_address        = to_address
        self.team_addresses    = team_addresses or []
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

        Always sent to the primary developer only (approval decisions should
        not be made by committee — one person, first click wins).

        The buttons link to:
          {approval_base_url}/approve?id={approval_id}
          {approval_base_url}/deny?id={approval_id}

        The ApprovalServer handles those GET requests and resolves the
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
        await self._send(
            recipients=[self.to_address],
            subject=subject,
            html=html,
            high_priority=True,
        )

    # ── Alert / notification flow ─────────────────────────────────────────────

    async def send_alert(
        self,
        title:   str,
        message: str,
        urgent:  bool = False,
    ) -> None:
        """
        Send a one-way alert notification.

        urgent=False  →  sent only to the primary developer (EMAIL_TO).
        urgent=True   →  emergency lane: sent to EMAIL_TO + every address in
                         EMAIL_TEAM simultaneously (individual SMTP sends so
                         each recipient sees only their own address in To:).

        urgent=True is set by the orchestrator when severity is HIGH or CRITICAL,
        or when the title contains "failed", "critical", or "manual action".
        """
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

    {'<p style="background:#fff3cd;border:1px solid #ffc107;padding:10px 16px;'
     'border-radius:4px;color:#856404;font-weight:bold;font-size:13px;">'
     '⚠️ HIGH / CRITICAL severity — full team notified</p>' if urgent else ''}

    <p style="color:#555;line-height:1.7;white-space:pre-wrap;">{message}</p>

    <p style="color:#aaa;font-size:11px;margin-top:30px;">
      Sent by DevOps Agent at {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}
    </p>
  </div>
</body>
</html>
"""
        # Build recipient list
        recipients = [self.to_address]
        if urgent and self.team_addresses:
            # Add team members not already in the list
            for addr in self.team_addresses:
                if addr and addr != self.to_address:
                    recipients.append(addr)
            logger.info(
                "[EmailClient] Emergency lane: broadcasting to %d recipients",
                len(recipients),
            )

        # Send to each recipient individually (To: shows only their address)
        tasks = [
            self._send(
                recipients=[addr],
                subject=subject,
                html=html,
                high_priority=urgent,
            )
            for addr in recipients
        ]
        await asyncio.gather(*tasks, return_exceptions=True)

    # ── SMTP helper ───────────────────────────────────────────────────────────

    async def _send(
        self,
        recipients:    List[str],
        subject:       str,
        html:          str,
        high_priority: bool = False,
    ) -> None:
        """Dispatch SMTP send in a thread so it doesn't block the event loop."""
        await asyncio.to_thread(
            self._send_sync,
            recipients=recipients,
            subject=subject,
            html=html,
            high_priority=high_priority,
        )

    def _send_sync(
        self,
        recipients:    List[str],
        subject:       str,
        html:          str,
        high_priority: bool = False,
    ) -> None:
        """Blocking SMTP send — called via asyncio.to_thread()."""
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
                with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=15) as server:
                    if self.use_tls:
                        server.starttls()
                    server.login(self.username, self.password)
                    server.sendmail(self.from_address, to_addr, msg.as_string())
                logger.info("[EmailClient] sent: %s → %s", subject, to_addr)
            except smtplib.SMTPException as exc:
                logger.error("[EmailClient] SMTP error (%s): %s", to_addr, exc)
            except Exception as exc:
                logger.error("[EmailClient] unexpected error (%s): %s", to_addr, exc)