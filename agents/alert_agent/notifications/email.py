"""
Alert Agent - Email Notification Provider
Handles sending normal alerts, urgent alerts, and approval request messages
mirroring the interface of SlackProvider.
"""

import logging
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from models import (
    ErrorEvent, RoutingDecision, ApprovalRequest,
    Solution, NotificationChannel, FileModification, ApprovalStatus
)

logger = logging.getLogger(__name__)


class EmailProvider:
    """
    Sends messages via SMTP email.

    Supports:
    - Normal alerts
    - Urgent alerts (high-priority header + subject prefix)
    - Approval request messages (approve / deny links)
    - Solution summary messages
    """

    def __init__(
        self,
        smtp_host: str,
        smtp_port: int,
        username: str,
        password: str,
        from_address: str,
        to_address: str,
        approval_address: str = None,
        use_tls: bool = True,
        approval_base_url: str = "",
    ):
        """
        Args:
            smtp_host:        SMTP server hostname.
            smtp_port:        SMTP server port (e.g. 587 for STARTTLS).
            username:         SMTP login username.
            password:         SMTP login password.
            from_address:     Sender email address.
            to_address:       Default recipient for alerts.
            approval_address: Recipient for approval requests (defaults to to_address).
            use_tls:          Whether to use STARTTLS (default True).
            approval_base_url: Base URL used to build approve/deny action links.
        """
        self.smtp_host        = smtp_host
        self.smtp_port        = smtp_port
        self.username         = username
        self.password         = password
        self.from_address     = from_address
        self.to_address       = to_address
        self.approval_address = approval_address or to_address
        self.use_tls          = use_tls
        self.approval_base_url = approval_base_url.rstrip("/")

    # ─────────────────────────────────────────
    # Public methods
    # ─────────────────────────────────────────

    def send_alert(self, event: ErrorEvent, decision: RoutingDecision) -> str:
        """
        Send an initial alert email based on the routing decision.
        Returns the Message-ID of the sent email.
        """
        is_urgent = NotificationChannel.SLACK_URGENT in decision.channels
        prefix    = "🚨 URGENT" if is_urgent else "⚠️ Alert"
        subject   = f"[{prefix}] {event.service}"

        body = self._build_alert_body(event, decision, prefix)
        return self._send_message(
            to=self.to_address,
            subject=subject,
            body=body,
            high_priority=is_urgent,
        )

    def send_approval_request(self, approval: ApprovalRequest) -> str:
        """
        Send an approval request email with Approve / Deny action links.
        Returns the Message-ID of the sent email.
        """
        labels = {
            "before_rag":     "Proceed with RAG Investigation",
            "before_healing": "Run Self-Healing Agent",
            "before_apply":   "Apply File Changes to Disk",
        }
        approval_type_label = labels.get(approval.approval_type, approval.approval_type)

        subject = f"[Approval Required] {approval_type_label} — {approval.event.service}"
        body    = self._build_approval_body(approval, approval_type_label)
        return self._send_message(
            to=self.approval_address,
            subject=subject,
            body=body,
            high_priority=True,
        )

    def send_solution_summary(self, event: ErrorEvent, solution: Solution) -> str:
        """
        Send the RAG solution summary to the engineer.
        Text only — no file modifications yet (Self-Healing hasn't run),
        no approval links yet (approval comes after file preview).
        """
        subject = f"[Solution Found] {event.service}"
        body    = self._build_solution_body(event, solution)
        return self._send_message(
            to=self.approval_address,
            subject=subject,
            body=body,
        )

    def send_file_modifications(
        self,
        event: ErrorEvent,
        modifications: list["FileModification"],
    ) -> None:
        """
        Send one email per modified file showing Before / After blocks.
        Called AFTER Self-Healing prepares fixes and BEFORE the apply approval,
        so the engineer can review exactly what will be written to disk.
        """
        for mod in modifications:
            before_text = mod.before[:1400] + "\n... (truncated)" if len(mod.before) > 1400 else mod.before
            after_text  = mod.after[:1400]  + "\n... (truncated)" if len(mod.after)  > 1400 else mod.after

            subject = f"[File Preview] {mod.file_path} — {event.service}"
            body    = self._build_file_modification_body(mod, before_text, after_text)
            self._send_message(to=self.approval_address, subject=subject, body=body)

    def send_resolution(
        self,
        event: ErrorEvent,
        fix_commands: list[str],
        modifications: list["FileModification"],
    ) -> str:
        """Send a resolution email after the fix was successfully applied."""
        commands_text = "\n".join(f"  • {cmd}" for cmd in fix_commands)
        files_text    = "\n".join(f"  • {m.file_path}" for m in modifications)

        lines = [f"✅ Resolved — {event.service}\n"]
        if fix_commands:
            lines.append(f"Commands applied:\n{commands_text}\n")
        if modifications:
            lines.append(f"Files updated:\n{files_text}\n")
        lines.append(f"Resolved at {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")

        subject = f"[Resolved] {event.service}"
        body    = "\n".join(lines)
        return self._send_message(to=self.to_address, subject=subject, body=body)

    def send_pipeline_stopped(
        self,
        event: ErrorEvent,
        approval_type: str,
        status: ApprovalStatus,
    ) -> None:
        """Notify engineer that the pipeline was stopped due to denial or timeout."""
        reason = "denied" if status.value == "denied" else "timed out"
        labels = {
            "before_rag":     "RAG investigation",
            "before_healing": "Self-Healing",
            "before_apply":   "applying files to disk",
        }
        label = labels.get(approval_type, approval_type)

        subject = f"[Pipeline Stopped] {event.service}"
        body    = (
            f"🛑 Pipeline Stopped — {event.service}\n\n"
            f"Approval for '{label}' was {reason}. No changes were made."
        )
        self._send_message(to=self.to_address, subject=subject, body=body)

    def send_forwarded_approval(
        self,
        approval: ApprovalRequest,
        target_address: str,
        forwarded_by: str,          # Email address of the engineer who forwarded
    ) -> str:
        """
        Send the forwarded approval to the target address (team or lead).
        No Forward link on this message — one hop only.
        """
        approval_type_label = {
            "before_rag":     "Proceed with RAG Investigation",
            "before_healing": "Run Self-Healing Agent",
            "before_apply":   "Apply File Changes to Disk",
        }.get(approval.approval_type, approval.approval_type)

        forward_note = f"Forwarded by {forwarded_by} from DevOps engineer queue\n\n"
        body = forward_note + self._build_approval_body(
            approval, approval_type_label, include_forward_links=False
        )
        subject = f"[Forwarded Approval] {approval_type_label} — {approval.event.service}"
        return self._send_message(to=target_address, subject=subject, body=body, high_priority=True)

    def send_forward_confirmation(
        self,
        original_message_id: str,
        target: str,
        forwarded_by: str,
    ) -> None:
        """
        Send a follow-up email confirming the approval was forwarded.
        (Analogous to updating the original Slack message.)
        """
        target_label = "Team" if target == "team" else "Lead"
        subject = f"[Forwarded] Approval sent to {target_label}"
        body    = (
            f"↗️ Forwarded to {target_label} by {forwarded_by}.\n\n"
            f"Original message ID: {original_message_id}"
        )
        self._send_message(to=self.approval_address, subject=subject, body=body)

    # ─────────────────────────────────────────
    # Body builders (keep message structure in one place)
    # ─────────────────────────────────────────

    def _build_alert_body(
        self, event: ErrorEvent, decision: RoutingDecision, prefix: str
    ) -> str:
        return (
            f"{prefix} — {event.service}\n\n"
            f"Message:   {event.message}\n"
            f"Severity:  {event.severity.upper()}\n"
            f"Impact:    {event.impact.upper()}\n"
            f"Frequency: {event.frequency}x\n"
            f"Interval:  {event.timing} min\n\n"
            f"Decision: {decision.reason}"
        )

    def _build_approval_body(
        self,
        approval: ApprovalRequest,
        approval_type_label: str,
        include_forward_links: bool = True,
    ) -> str:
        event = approval.event
        base  = self.approval_base_url

        lines = [
            f"🔐 Approval Required — {event.service}\n",
            f"Action:      {approval_type_label}",
            f"Reason:      {approval.decision.reason}",
            f"Approval ID: {approval.approval_id}",
            "",
            f"  ✅ Approve: {base}/approve?id={approval.approval_id}&type={approval.approval_type}",
            f"  ❌ Deny:    {base}/deny?id={approval.approval_id}&type={approval.approval_type}",
        ]

        if include_forward_links and approval.can_forward:
            lines += [
                "",
                f"  → Forward to Team: {base}/forward?id={approval.approval_id}&type={approval.approval_type}&target=team",
                f"  → Forward to Lead: {base}/forward?id={approval.approval_id}&type={approval.approval_type}&target=lead",
            ]

        return "\n".join(lines)

    def _build_solution_body(self, event: ErrorEvent, solution: Solution) -> str:
        """
        Solution summary body — text only.
        No file modifications (Self-Healing hasn't run yet).
        No approval links (those come after the file preview).
        """
        commands_text = "\n".join(f"  • {cmd}" for cmd in solution.fix_commands)
        body = (
            f"🔍 Solution Found — {event.service}\n\n"
            f"Possible Cause:   {solution.possible_cause}\n"
            f"Recommended Fix:  {solution.recommended_fix}\n"
            f"Confidence:       {solution.confidence:.0%}"
        )
        if solution.fix_commands:
            body += f"\n\nShell Commands:\n{commands_text}"
        return body

    def _build_file_modification_body(
        self, mod: "FileModification", before_text: str, after_text: str
    ) -> str:
        description = f"\n{mod.description}" if mod.description else ""
        return (
            f"📄 File: {mod.file_path}{description}\n\n"
            f"— Before —\n"
            f"{before_text}\n\n"
            f"— After —\n"
            f"{after_text}"
        )

    # ─────────────────────────────────────────
    # SMTP helpers
    # ─────────────────────────────────────────

    def _send_message(
        self,
        to: str,
        subject: str,
        body: str,
        high_priority: bool = False,
    ) -> str:
        """Send an email via SMTP and return the Message-ID."""
        msg = MIMEMultipart("alternative")
        msg["From"]    = self.from_address
        msg["To"]      = to
        msg["Subject"] = subject

        if high_priority:
            msg["X-Priority"] = "1"
            msg["Importance"] = "high"

        msg.attach(MIMEText(body, "plain"))

        try:
            with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
                if self.use_tls:
                    server.starttls()
                server.login(self.username, self.password)
                server.sendmail(self.from_address, to, msg.as_string())

            message_id = msg.get("Message-ID", "")
            logger.info("Email sent to %s (subject=%r)", to, subject)
            return message_id

        except smtplib.SMTPException as exc:
            logger.error("Email send failed: %s", exc)
            return ""