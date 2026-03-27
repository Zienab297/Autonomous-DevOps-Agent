"""
core/slack_client.py
────────────────────
Async Slack client for the DevOps SDK.

Handles two concerns:
  1. Approval messages  — Block Kit messages with ✅ Approve / ❌ Deny buttons
                          posted to SLACK_APPROVAL_CHANNEL.
  2. Alert messages     — One-way notifications (deployment done, incident
                          resolved, remediation failed) posted to SLACK_CHANNEL.

Adapted from agents/alert_agent/notifications/slack.py but:
  • Uses httpx.AsyncClient (non-blocking — fits the SDK's asyncio event loop).
  • No FastAPI / routing dependency.
  • Drops the forward-to-team/lead flow (not needed in the SDK approval gates).
  • Exposes a minimal interface that ApprovalManager and Orchestrator need.

Configuration (.env):
    SLACK_BOT_TOKEN        xoxb-...
    SLACK_CHANNEL          #devops-alerts          (one-way alerts)
    SLACK_APPROVAL_CHANNEL #devops-approvals        (approval requests)
"""

import json
import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_SLACK_API = "https://slack.com/api"


class SlackClient:
    """
    Async Slack Web-API wrapper.

    All public methods are async and safe to await inside the SDK's event loop.
    """

    def __init__(
        self,
        bot_token:        str,
        channel:          str,            # alert channel  (#devops-alerts)
        approval_channel: str,            # approval channel (#devops-approvals)
    ):
        self.channel          = channel
        self.approval_channel = approval_channel
        self._headers = {
            "Authorization": f"Bearer {bot_token}",
            "Content-Type":  "application/json",
        }

    # ── Approval flow ─────────────────────────────────────────────────────────

    async def send_approval_request(
        self,
        approval_id: str,
        title:       str,
        details:     list[str],
    ) -> str:
        """
        Post an approval request with ✅ Approve and ❌ Deny buttons.

        Returns the Slack message timestamp (ts) — stored so we can update
        the message once the engineer clicks.
        """
        detail_text = "\n".join(f"  • {d}" for d in details)

        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"🔐 *Approval Required*\n"
                        f"*{title}*\n\n"
                        f"{detail_text}"
                    ),
                },
            },
            {"type": "divider"},
            {
                "type": "actions",
                "elements": [
                    {
                        "type":      "button",
                        "text":      {"type": "plain_text", "text": "✅  Approve"},
                        "style":     "primary",
                        "action_id": "devops_approve",
                        "value":     json.dumps({"approval_id": approval_id}),
                    },
                    {
                        "type":      "button",
                        "text":      {"type": "plain_text", "text": "❌  Deny"},
                        "style":     "danger",
                        "action_id": "devops_deny",
                        "value":     json.dumps({"approval_id": approval_id}),
                    },
                ],
            },
        ]
        return await self._post(self.approval_channel, blocks)

    async def update_approval_message(
        self,
        ts:          str,
        approved:    bool,
        resolved_by: str = "unknown",
    ) -> None:
        """Replace the approval buttons with a decision banner after a click."""
        icon  = "✅" if approved else "❌"
        label = "Approved" if approved else "Denied"
        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"{icon} *{label}* by <@{resolved_by}>",
                },
            }
        ]
        await self._update(self.approval_channel, ts, blocks)

    async def wait_for_response(self, approval_id: str) -> bool:
        """
        NOT polled here — the ApprovalServer calls resolve() directly.
        This method exists so ApprovalManager can treat Slack and Email
        symmetrically.  Returns value is set via resolve_approval().
        """
        raise NotImplementedError(
            "SlackClient.wait_for_response() should never be called directly. "
            "The ApprovalServer resolves approvals via resolve_approval()."
        )

    # ── Alert / notification flow ─────────────────────────────────────────────

    async def send_alert(
        self,
        title:   str,
        message: str,
        urgent:  bool = False,
    ) -> str:
        """
        Send a one-way alert notification.

        urgent=True adds <!channel> and a red header — used for incidents
        and remediation failures.
        """
        mention = "<!channel> " if urgent else ""
        icon    = "🚨" if urgent else "📢"

        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"{mention}{icon} *{title}*\n{message}",
                },
            },
            {"type": "divider"},
        ]
        return await self._post(self.channel, blocks)

    # ── HTTP helpers ──────────────────────────────────────────────────────────

    async def _post(self, channel: str, blocks: list[dict]) -> str:
        """POST chat.postMessage; returns the message ts or '' on error."""
        payload = {"channel": channel, "blocks": blocks}
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{_SLACK_API}/chat.postMessage",
                    headers=self._headers,
                    json=payload,
                )
            data = resp.json()
            if not data.get("ok"):
                logger.error("[SlackClient] postMessage failed: %s", data.get("error"))
                return ""
            ts = data.get("ts", "")
            logger.info("[SlackClient] message sent to %s (ts=%s)", channel, ts)
            return ts
        except Exception as exc:
            logger.error("[SlackClient] postMessage exception: %s", exc)
            return ""

    async def _update(self, channel: str, ts: str, blocks: list[dict]) -> None:
        """POST chat.update to replace an existing message."""
        payload = {"channel": channel, "ts": ts, "blocks": blocks}
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{_SLACK_API}/chat.update",
                    headers=self._headers,
                    json=payload,
                )
            data = resp.json()
            if not data.get("ok"):
                logger.error("[SlackClient] chat.update failed: %s", data.get("error"))
        except Exception as exc:
            logger.error("[SlackClient] chat.update exception: %s", exc)