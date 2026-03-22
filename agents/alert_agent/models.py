"""
Alert Agent - Data Models
Defines the core data structures used across the alert agent.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
from datetime import datetime


# ─────────────────────────────────────────────
# Enums
# ─────────────────────────────────────────────

class Severity(str, Enum):
    LOW      = "low"
    MEDIUM   = "medium"
    HIGH     = "high"
    CRITICAL = "critical"


class Impact(str, Enum):
    LOW    = "low"
    MEDIUM = "medium"
    HIGH   = "high"


class NotificationChannel(str, Enum):
    SLACK_NORMAL  = "slack_normal"   # normal slack message
    SLACK_URGENT  = "slack_urgent"   # @channel / urgent slack
    EMAIL         = "email"
    PHONE_CALL    = "phone_call"


class ApprovalStatus(str, Enum):
    PENDING   = "pending"
    APPROVED  = "approved"
    DENIED    = "denied"
    FORWARDED = "forwarded"   # engineer forwarded to team/lead — not a final answer


class AlertAction(str, Enum):
    """What the agent should do after routing."""
    NOTIFY_AND_ROLLBACK                  = "notify_and_rollback"                   # low/low / 1-5 freq / >10min
    NOTIFY_NORMAL_AND_WAIT_APPROVAL      = "notify_normal_and_wait_approval"       # low/low/>6/<5min  +  medium
    NOTIFY_URGENT_AND_WAIT_APPROVAL      = "notify_urgent_and_wait_approval"       # high
    NOTIFY_CRITICAL_AND_WAIT_APPROVAL    = "notify_critical_and_wait_approval"     # critical


# ─────────────────────────────────────────────
# Core models
# ─────────────────────────────────────────────

@dataclass
class ErrorEvent:
    """
    Incoming error received by the Alert Agent.

    Attributes:
        service:    Name of the affected service.
        severity:   How severe the error is.
        impact:     How much it impacts users / system.
        frequency:  How many times it occurred.
        timing:     Minutes between occurrences (interval).
        message:    Human-readable error description.
        timestamp:  When the event was created.
    """
    service:   str
    severity:  Severity
    impact:    Impact
    frequency: int
    timing:    float                      # minutes between occurrences
    message:   str
    timestamp: datetime = field(default_factory=datetime.utcnow)
    extra:     dict     = field(default_factory=dict)  # any additional metadata


@dataclass
class RoutingDecision:
    """
    Result produced by the RoutingEngine for a given ErrorEvent.

    Attributes:
        action:       What the agent should do.
        channels:     Which channels to notify.
        requires_approval_before_rag:    Whether approval is needed before RAG.
        requires_approval_before_apply:  Whether approval is needed before applying fix.
        reason:       Human-readable explanation of why this decision was made.
    """
    action:                        AlertAction
    channels:                      list[NotificationChannel]
    requires_approval_before_rag:  bool
    requires_approval_before_healing: bool
    requires_approval_before_apply: bool
    reason:                        str


@dataclass
class ApprovalRequest:
    """
    Sent to Slack when the agent needs engineer approval.

    Attributes:
        approval_id:   Unique ID used to match the callback.
        event:         The original error event.
        decision:      The routing decision that triggered this approval.
        approval_type: "before_rag" or "before_healing" or "before_apply"
        status:        Current approval status.
        slack_ts:      Slack message timestamp (used to update the message).
    """
    approval_id:   str
    event:         ErrorEvent
    decision:      RoutingDecision
    approval_type: str                              # "before_rag" | "before_healing" |  "before_apply"
    status:        ApprovalStatus = ApprovalStatus.PENDING
    can_forward:   bool           = True            # False on forwarded approvals (one hop only)
    slack_ts:      Optional[str]  = None
    resolved_at:   Optional[datetime] = None
    resolved_by:   Optional[str]      = None       # Slack user who clicked the button


@dataclass
class FileModification:
    """
    A single file change proposed by the Self-Healing Agent.
    Shown to the engineer (before/after) before applying to disk.

    Attributes:
        file_path:   Path of the file to modify.
        before:      Current file content (or relevant section).
        after:       Proposed new content.
        description: One-line summary of what this change does.
    """
    file_path:   str
    before:      str
    after:       str
    description: str = ""


@dataclass
class Solution:
    """
    Solution produced by the RAG / Knowledge Agent.
    Text only — no file changes yet. File modifications
    are produced later by the Self-Healing Agent.

    Attributes:
        possible_cause:  What the agent thinks caused the issue.
        recommended_fix: What action to take.
        confidence:      Confidence score 0.0 - 1.0.
        fix_commands:    Shell commands (e.g. kubectl restart).
        raw_response:    Raw LLM output for debugging.
    """
    possible_cause:  str
    recommended_fix: str
    confidence:      float
    fix_commands:    list[str] = field(default_factory=list)
    raw_response:    str       = ""