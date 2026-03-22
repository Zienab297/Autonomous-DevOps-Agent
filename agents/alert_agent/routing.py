"""
Alert Agent - Routing Engine
Maps an ErrorEvent to a RoutingDecision using a simple rules table.

Rules table (evaluated top-to-bottom, first match wins):
┌──────────┬────────┬───────────┬───────────┬───────────────────────────────┬──────────────────────────────┐
│ Severity │ Impact │ Frequency │  Timing   │ Action                        │ Channels                     │
├──────────┼────────┼───────────┼───────────┼───────────────────────────────┼──────────────────────────────┤
│ low      │ low    │  1–5      │ > 10 min  │ notify + rollback             │ slack_normal                 │
│ low      │ low    │  > 6      │ < 5 min   │ notify + wait approval        │ slack_normal                 │
│ medium   │ medium │  any      │  any      │ notify normal                 │ slack_normal + email         │
│ high     │ high   │  any      │  any      │ notify urgent                 │ slack_urgent + email         │
│ critical │  any   │  any      │  any      │ notify critical               │ slack_urgent + email + call  │
└──────────┴────────┴───────────┴───────────┴───────────────────────────────┴──────────────────────────────┘
"""

from models import (
    ErrorEvent, RoutingDecision,
    Severity, Impact,
    NotificationChannel, AlertAction,
)


# ─────────────────────────────────────────────
# Rules Table
# Each rule is a dict with:
#   match   – callable(ErrorEvent) -> bool
#   result  – RoutingDecision fields
# ─────────────────────────────────────────────

ROUTING_RULES: list[dict] = [

    # ── Rule 1: low/low, infrequent (1-5), wide interval (>10 min) ──
    # Safe to auto-rollback, just notify engineer.
    {
        "match": lambda e: (
            e.severity  == Severity.LOW
            and e.impact == Impact.LOW
            and 1 <= e.frequency <= 5
            and e.timing > 10
        ),
        "action":   AlertAction.NOTIFY_AND_ROLLBACK,
        "channels": [NotificationChannel.SLACK_NORMAL, NotificationChannel.EMAIL],
        "requires_approval_before_rag":   False,
        "requires_approval_before_healing":False,
        "requires_approval_before_apply": False,
        "reason": "Low severity, low impact, infrequent (1-5), interval > 10 min → auto rollback, notify engineer via Slack.",
    },

    # ── Rule 2: low/low, frequent (>6), tight interval (<5 min) ──
    # Happening too fast to auto-fix — ask engineer before continuing.
    {
        "match": lambda e: (
            e.severity  == Severity.LOW
            and e.impact == Impact.LOW
            and e.frequency > 6
            and e.timing < 5
        ),
        "action":   AlertAction.NOTIFY_NORMAL_AND_WAIT_APPROVAL,
        "channels": [NotificationChannel.SLACK_NORMAL, NotificationChannel.EMAIL],
        "requires_approval_before_rag":   True,
        "requires_approval_before_healing":True,
        "requires_approval_before_apply": True,
        "reason": "Low severity, low impact, frequent (>6), interval < 5 min → notify engineer, wait for approval before RAG and before applying fix.",
    },

    # ── Rule 3: medium/medium ──
    {
        "match": lambda e: (
            e.severity == Severity.MEDIUM
            and e.impact == Impact.MEDIUM
        ),
        "action":   AlertAction.NOTIFY_NORMAL_AND_WAIT_APPROVAL,
        "channels": [NotificationChannel.SLACK_NORMAL, NotificationChannel.EMAIL],
        "requires_approval_before_rag":   True,
        "requires_approval_before_healing":True,
        "requires_approval_before_apply": True,
        "reason": "Medium severity and impact → notify via Slack + Email, approval required before applying fix.",
    },

    # ── Rule 4: high/high ──
    {
        "match": lambda e: (
            e.severity == Severity.HIGH
            and e.impact == Impact.HIGH
        ),
        "action":   AlertAction.NOTIFY_URGENT_AND_WAIT_APPROVAL,
        "channels": [NotificationChannel.SLACK_URGENT, NotificationChannel.EMAIL],
        "requires_approval_before_rag":   True,
        "requires_approval_before_healing":True,
        "requires_approval_before_apply": True,
        "reason": "High severity and impact → urgent Slack + Email, approval required before applying fix.",
    },

    # ── Rule 5: critical (any impact) ──
    {
        "match": lambda e: e.severity == Severity.CRITICAL,
        "action":   AlertAction.NOTIFY_CRITICAL_AND_WAIT_APPROVAL,
        "channels": [
            NotificationChannel.SLACK_URGENT,
            NotificationChannel.EMAIL,
            NotificationChannel.PHONE_CALL,
        ],
        "requires_approval_before_rag":   True,
        "requires_approval_before_healing":True,
        "requires_approval_before_apply": True,
        "reason": "Critical severity → urgent Slack + Email + Phone call, approval required before applying fix.",
    },
]


# ─────────────────────────────────────────────
# Routing Engine
# ─────────────────────────────────────────────

class RoutingEngine:
    """
    Evaluates the ROUTING_RULES table against an ErrorEvent
    and returns the first matching RoutingDecision.
    """

    def route(self, event: ErrorEvent) -> RoutingDecision:
        """
        Find the first matching rule for the given event.

        Args:
            event: The incoming ErrorEvent.

        Returns:
            RoutingDecision with action, channels, approval flags, and reason.

        Raises:
            ValueError: If no rule matches (shouldn't happen with a catch-all).
        """
        for rule in ROUTING_RULES:
            if rule["match"](event):
                return RoutingDecision(
                    action=rule["action"],
                    channels=rule["channels"],
                    requires_approval_before_rag=rule["requires_approval_before_rag"],
                    requires_approval_before_healing=rule["requires_approval_before_healing"],
                    requires_approval_before_apply=rule["requires_approval_before_apply"],
                    reason=rule["reason"],
                )

        # Fallback — should not reach here if rules are complete
        raise ValueError(
            f"No routing rule matched event: severity={event.severity}, "
            f"impact={event.impact}, frequency={event.frequency}, timing={event.timing}"
        )