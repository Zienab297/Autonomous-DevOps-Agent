"""
Alert Agent - Slack Notification Provider
Handles sending normal alerts, urgent alerts, and approval request messages
with interactive Approve / Deny buttons.
"""

import json
import logging
from datetime import datetime

import httpx

from models import (
    ErrorEvent, RoutingDecision, ApprovalRequest,
    Solution, NotificationChannel, FileModification, ApprovalStatus
)

logger = logging.getLogger(__name__)


class SlackProvider:
    """
    Sends messages to Slack using the Web API.

    Supports:
    - Normal alerts
    - Urgent alerts (@channel mention)
    - Approval request messages with interactive buttons
    - Solution summary messages
    """

    def __init__(self, bot_token: str, channel: str, approval_channel: str = None):
        """
        Args:
            bot_token:        Slack bot OAuth token (xoxb-...).
            channel:          Default channel for alerts (e.g. #devops-alerts).
            approval_channel: Channel for approval requests (defaults to same channel).
        """
        self.bot_token        = bot_token
        self.channel          = channel
        self.approval_channel = approval_channel or channel
        self.base_url         = "https://slack.com/api"
        self.headers          = {
            "Authorization": f"Bearer {bot_token}",
            "Content-Type":  "application/json",
        }

    # ─────────────────────────────────────────
    # Public methods
    # ─────────────────────────────────────────

    def send_alert(self, event: ErrorEvent, decision: RoutingDecision) -> str:
        """
        Send an initial alert message based on the routing decision.
        Returns the Slack message timestamp (ts).
        """
        is_urgent = NotificationChannel.SLACK_URGENT in decision.channels
        prefix    = "🚨 *URGENT*" if is_urgent else "⚠️ *Alert*"
        mention   = "<!channel> " if is_urgent else ""

        blocks = self._build_alert_blocks(event, decision, prefix, mention)
        return self._post_message(self.channel, blocks)

    def send_approval_request(self, approval: ApprovalRequest) -> str:
        """
        Send an approval request message with Approve / Deny buttons.
        Returns the Slack message timestamp (ts).
        """
        labels = {
            "before_rag":     "Proceed with RAG Investigation",
            "before_healing": "Run Self-Healing Agent",
            "before_apply":   "Apply File Changes to Disk",
        }
        approval_type_label = labels.get(approval.approval_type, approval.approval_type)

        blocks = self._build_approval_blocks(approval, approval_type_label)
        return self._post_message(self.approval_channel, blocks)

    def send_solution_summary( self, event: ErrorEvent, solution: Solution, ) -> str:
        """
        Send the RAG solution summary to the engineer.
        Text only — no file modifications yet (Self-Healing hasn't run),
        no approval buttons yet (approval comes after file preview).
        """
        blocks = self._build_solution_blocks(event, solution)
        return self._post_message(self.approval_channel, blocks)

    def send_file_modifications( self, event: ErrorEvent, modifications: list["FileModification"], ) -> None:
        """
        Send one Slack message per modified file showing Before / After blocks.
        Called AFTER Self-Healing prepares fixes and BEFORE the apply approval,
        so the engineer can review exactly what will be written to disk.
        """
        for mod in modifications:
            before_text = mod.before[:1400] + "\n... (truncated)" if len(mod.before) > 1400 else mod.before
            after_text  = mod.after[:1400]  + "\n... (truncated)" if len(mod.after)  > 1400 else mod.after

            blocks = [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"📄 *File:* `{mod.file_path}`\n"
                            f"_{mod.description}_" if mod.description else f"📄 *File:* `{mod.file_path}`"
                        ),
                    },
                },
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"*— Before —*\n```{before_text}```"},
                },
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"*— After —*\n```{after_text}```"},
                },
                {"type": "divider"},
            ]
            self._post_message(self.approval_channel, blocks)

    def send_resolution(self, event:         ErrorEvent, fix_commands:  list[str], modifications: list["FileModification"], ) -> str:
        """Send a resolution message after the fix was successfully applied."""
        commands_text = "\n".join(f"  • `{cmd}`" for cmd in fix_commands)
        files_text    = "\n".join(f"  • `{m.file_path}`" for m in modifications)

        text = f"✅ *Resolved* — `{event.service}`\n"
        if fix_commands:
            text += f"*Commands applied:*\n{commands_text}\n"
        if modifications:
            text += f"*Files updated:*\n{files_text}\n"
        text += f"_Resolved at {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}_"

        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
        return self._post_message(self.channel, blocks)

    def send_pipeline_stopped( self, event: ErrorEvent, approval_type: str, status: ApprovalStatus, ) -> None:
        """Notify engineer that the pipeline was stopped due to denial or timeout."""
        reason = "denied" if status.value == "denied" else "timed out"
        labels = {
            "before_rag":      "RAG investigation",
            "before_healing":  "Self-Healing",
            "before_apply":    "applying files to disk",
        }
        label = labels.get(approval_type, approval_type)
        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"🛑 *Pipeline Stopped* — `{event.service}`\n"
                        f"Approval for *{label}* was {reason}. No changes were made."
                    ),
                },
            }
        ]
        self._post_message(self.channel, blocks)

    def send_forwarded_approval( self, approval: ApprovalRequest, target_channel: str, 
                                forwarded_by:   str,           # Slack user ID of the engineer who forwarded
        ) -> str:
        """
        Send the forwarded approval to the target channel (team or lead).
        No Forward button on this message — one hop only.
        """
        approval_type_label = {
            "before_rag":     "Proceed with RAG Investigation",
            "before_healing": "Run Self-Healing Agent",
            "before_apply":   "Apply File Changes to Disk",
        }.get(approval.approval_type, approval.approval_type)
        event = approval.event

        # Context block explaining who forwarded it
        context_block = {
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": f"Forwarded by <@{forwarded_by}> from DevOps engineer queue",
            }],
        }

        blocks = [context_block] + self._build_approval_blocks(approval, approval_type_label)
        return self._post_message(target_channel, blocks)

    def send_forward_confirmation(self, original_ts: str, target: str, forwarded_by: str) -> None:
        """Update the original approval message to show it was forwarded."""
        target_label = "Team" if target == "team" else "Lead"
        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"↗️ *Forwarded to {target_label}* by <@{forwarded_by}>",
                },
            }
        ]
        self._update_message(self.approval_channel, original_ts, blocks)

    def update_approval_message(self, ts: str, status: str, resolved_by: str) -> None:
        """Update the approval message to show Approved / Denied after the engineer clicks."""
        icon  = "✅" if status == "approved" else "❌"
        label = "Approved" if status == "approved" else "Denied"
        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"{icon} *{label}* by <@{resolved_by}>",
                },
            }
        ]
        self._update_message(self.approval_channel, ts, blocks)

    # ─────────────────────────────────────────
    # Block builders (keep message structure in one place)
    # ─────────────────────────────────────────

    def _build_alert_blocks( self, event: ErrorEvent, decision: RoutingDecision, prefix: str, mention: str, ) -> list[dict]:
        return [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"{mention}{prefix} — `{event.service}`\n"
                        f"*Message:* {event.message}\n"
                        f"*Severity:* `{event.severity.upper()}` | "
                        f"*Impact:* `{event.impact.upper()}` | "
                        f"*Frequency:* {event.frequency}x | "
                        f"*Interval:* {event.timing} min\n"
                        f"*Decision:* {decision.reason}"
                    ),
                },
            },
            {"type": "divider"},
        ]

    def _build_approval_blocks( self, approval: ApprovalRequest, approval_type_label: str,) -> list[dict]:
        event = approval.event

        # Base info block
        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"🔐 *Approval Required* — `{event.service}`\n"
                        f"*Action:* {approval_type_label}\n"
                        f"*Reason:* {approval.decision.reason}\n"
                        f"*Approval ID:* `{approval.approval_id}`"
                    ),
                },
            },
        ]

        # Build action buttons
        buttons = [
            {
                "type": "button",
                "text":      {"type": "plain_text", "text": "✅ Approve"},
                "style":     "primary",
                "action_id": "approval_approve",
                "value":     json.dumps({
                    "approval_id":   approval.approval_id,
                    "approval_type": approval.approval_type,
                }),
            },
            {
                "type": "button",
                "text":      {"type": "plain_text", "text": "❌ Deny"},
                "style":     "danger",
                "action_id": "approval_deny",
                "value":     json.dumps({
                    "approval_id":   approval.approval_id,
                    "approval_type": approval.approval_type,
                }),
            },
        ]

        # Add Forward buttons only if this approval allows forwarding
        if approval.can_forward:
            buttons.append({
                "type":      "button",
                "text":      {"type": "plain_text", "text": "→ Forward to Team"},
                "action_id": "approval_forward_team",
                "value":     json.dumps({
                    "approval_id":   approval.approval_id,
                    "approval_type": approval.approval_type,
                    "target":        "team",
                }),
            })
            buttons.append({
                "type":      "button",
                "text":      {"type": "plain_text", "text": "→ Forward to Lead"},
                "action_id": "approval_forward_lead",
                "value":     json.dumps({
                    "approval_id":   approval.approval_id,
                    "approval_type": approval.approval_type,
                    "target":        "lead",
                }),
            })

        blocks.append({"type": "actions", "elements": buttons})
        return blocks


    def _build_solution_blocks( self, event: ErrorEvent, solution: "Solution", ) -> list[dict]:
        """
        Solution summary block — text only.
        No file modifications (Self-Healing hasn't run yet).
        No approval buttons (those come after the file preview).
        """
        commands_text = "\n".join(f"  • `{cmd}`" for cmd in solution.fix_commands)

        return [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"🔍 *Solution Found* — `{event.service}`\n"
                        f"*Possible Cause:* {solution.possible_cause}\n"
                        f"*Recommended Fix:* {solution.recommended_fix}\n"
                        f"*Confidence:* {solution.confidence:.0%}"
                        + (f"\n*Shell Commands:*\n{commands_text}" if solution.fix_commands else "")
                    ),
                },
            },
        ]

    # ─────────────────────────────────────────
    # HTTP helpers
    # ─────────────────────────────────────────

    def _post_message(self, channel: str, blocks: list[dict]) -> str:
        """Post a message to Slack and return the message ts."""
        payload = {"channel": channel, "blocks": blocks}
        response = httpx.post(
            f"{self.base_url}/chat.postMessage",
            headers=self.headers,
            json=payload,
            timeout=10,
        )
        data = response.json()

        if not data.get("ok"):
            logger.error("Slack postMessage failed: %s", data.get("error"))
        else:
            logger.info("Slack message sent to %s (ts=%s)", channel, data.get("ts"))

        return data.get("ts", "")

    def _update_message(self, channel: str, ts: str, blocks: list[dict]) -> None:
        """Update an existing Slack message."""
        payload = {"channel": channel, "ts": ts, "blocks": blocks}
        response = httpx.post(
            f"{self.base_url}/chat.update",
            headers=self.headers,
            json=payload,
            timeout=10,
        )
        data = response.json()
        if not data.get("ok"):
            logger.error("Slack update failed: %s", data.get("error"))