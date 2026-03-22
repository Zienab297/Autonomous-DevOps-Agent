"""
Alert Agent - Slack Interactive Webhook Handler (FastAPI)
Receives button click callbacks from Slack when an engineer
clicks Approve or Deny on an approval message.

Register this router in your main FastAPI app:
    from alert_agent.agent.slack_webhook import build_slack_router
    app.include_router(build_slack_router(approval_manager, slack_provider))
"""

import json
import logging
import urllib.parse

from fastapi import APIRouter, Request, Response

from models import ApprovalStatus
from approval    import ApprovalManager
from notifications.slack import SlackProvider

logger = logging.getLogger(__name__)


def build_slack_router(
    approval_manager: ApprovalManager,
    slack:            SlackProvider,
) -> APIRouter:
    """
    Factory that returns a FastAPI router with the /slack/interactive endpoint.
    Inject the shared ApprovalManager and SlackProvider instances.
    """
    router = APIRouter()

    @router.post("/slack/interactive")
    async def slack_interactive(request: Request) -> Response:
        """
        Slack sends a form-encoded payload when an engineer clicks a button.
        We parse it, find the approval_id, and resolve it.
        """
        # ── Parse Slack payload ────────────────────────────────────
        body    = await request.body()
        decoded = urllib.parse.unquote_plus(body.decode())

        # Slack sends: payload=<json>
        if not decoded.startswith("payload="):
            logger.warning("Unexpected Slack payload format")
            return Response(status_code=400)

        payload = json.loads(decoded[len("payload="):])

        # ── Extract action details ─────────────────────────────────
        actions    = payload.get("actions", [])
        user_id    = payload.get("user", {}).get("id", "unknown")
        message_ts = payload.get("message", {}).get("ts")

        if not actions:
            return Response(status_code=200)

        action     = actions[0]
        action_id  = action.get("action_id")
        value_json = action.get("value", "{}")
        value      = json.loads(value_json)

        approval_id   = value.get("approval_id")
        approval_type = value.get("approval_type")

        if not approval_id:
            logger.warning("Slack callback missing approval_id")
            return Response(status_code=200)

        # ── Route to the correct handler ───────────────────────────

        # Approve / Deny
        if action_id in ("approval_approve", "approval_deny"):
            status  = (
                ApprovalStatus.APPROVED
                if action_id == "approval_approve"
                else ApprovalStatus.DENIED
            )
            updated = approval_manager.resolve_approval(
                approval_id=approval_id,
                status=status,
                resolved_by=user_id,
            )
            if updated and message_ts:
                slack.update_approval_message(
                    ts=message_ts,
                    status=status.value,
                    resolved_by=user_id,
                )

        # Forward to Team or Lead
        elif action_id in ("approval_forward_team", "approval_forward_lead"):
            target = value.get("target", "team")   # "team" | "lead"

            result = approval_manager.forward_approval(
                original_approval_id=approval_id,
                target=target,
                forwarded_by=user_id,
            )

            if result:
                new_approval, target_channel = result

                # Update original message to show forwarded state
                if message_ts:
                    slack.send_forward_confirmation(
                        original_ts=message_ts,
                        target=target,
                        forwarded_by=user_id,
                    )

                # Send the new approval to the target channel
                new_ts = slack.send_forwarded_approval(
                    approval=new_approval,
                    target_channel=target_channel,
                    forwarded_by=user_id,
                )
                new_approval.slack_ts = new_ts

                logger.info(
                    "Forwarded approval [%s] → [%s] channel=%s",
                    approval_id, new_approval.approval_id, target_channel,
                )

        logger.info(
            "Slack interactive: approval_id=%s action=%s user=%s type=%s",
            approval_id, action_id, user_id, approval_type,
        )

        return Response(status_code=200)

    return router