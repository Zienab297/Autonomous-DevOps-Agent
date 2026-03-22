"""
Alert Agent - Manual Test Script

Tests all routing cases against real Slack and Email.
Run after filling in .env:

    python test_agent.py

Pick which case to test by passing an argument:
    python test_agent.py low_infrequent
    python test_agent.py low_frequent
    python test_agent.py medium
    python test_agent.py high
    python test_agent.py critical
    python test_agent.py all          ← runs all cases sequentially
"""

import asyncio
import logging
import os
import sys

from dotenv import load_dotenv

from models import (
    ErrorEvent, Severity, Impact, Solution, FileModification,
)
from notifications.slack import SlackProvider
from notifications.email import EmailProvider
from approval             import ApprovalManager
from main.agent                import AlertAgent

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────
# Build agent
# ─────────────────────────────────────────

def build_agent() -> AlertAgent:
    slack = SlackProvider(
        bot_token=os.environ["SLACK_BOT_TOKEN"],
        channel=os.environ["SLACK_CHANNEL"],
        approval_channel=os.environ["SLACK_APPROVAL_CHANNEL"],
    )

    email = EmailProvider(
        smtp_host=os.environ["SMTP_HOST"],
        smtp_port=int(os.environ["SMTP_PORT"]),
        username=os.environ["SMTP_USERNAME"],
        password=os.environ["SMTP_PASSWORD"],
        from_address=os.environ["EMAIL_FROM"],
        to_address=os.environ["EMAIL_TO"],
        approval_base_url=os.environ.get("APPROVAL_BASE_URL", ""),
    )

    approval_manager = ApprovalManager(
        timeout_seconds=int(os.environ.get("APPROVAL_TIMEOUT_SECONDS", 60)),  # short for testing
        team_channel=os.environ.get("SLACK_TEAM_CHANNEL", "#devops-team"),
        lead_channel=os.environ.get("SLACK_LEAD_CHANNEL", "#devops-leads"),
    )

    agent = AlertAgent(slack, email, approval_manager)

    # ── RAG handler ───────────────────────────────────────────────
    async def rag_handler(event: ErrorEvent) -> Solution:
        logger.info("[TEST] RAG handler running for service=%s", event.service)
        return Solution(
            possible_cause="Memory leak caused by unbounded cache growth in the request handler.",
            recommended_fix="Increase pod memory limit and patch the cache eviction policy.",
            confidence=0.91,
            fix_commands=[
                "kubectl rollout restart deployment/my-service",
                "kubectl set resources deployment/my-service --limits=memory=512Mi",
            ],
        )

    # ── Self-Healing prepare handler ──────────────────────────────
    async def self_healing_prepare_handler(
        event: ErrorEvent, solution: Solution
    ) -> list[FileModification]:
        logger.info("[TEST] Self-Healing prepare running for service=%s", event.service)
        return [
            FileModification(
                file_path="k8s/my-service/deployment.yaml",
                before="memory: 256Mi",
                after="memory: 512Mi",
                description="Increase memory limit to prevent OOM kills",
            ),
            FileModification(
                file_path="config/cache.yaml",
                before="max_size: unlimited",
                after="max_size: 1000",
                description="Add cache eviction limit",
            ),
        ]

    # ── Self-Healing apply handler ────────────────────────────────
    async def self_healing_apply_handler(
        event: ErrorEvent, modifications: list[FileModification]
    ) -> None:
        logger.info(
            "[TEST] Self-Healing apply — writing %d file(s) for service=%s",
            len(modifications), event.service,
        )
        for mod in modifications:
            logger.info("[TEST] Applied: %s", mod.file_path)

    agent.set_rag_handler(rag_handler)
    agent.set_self_healing_prepare_handler(self_healing_prepare_handler)
    agent.set_self_healing_apply_handler(self_healing_apply_handler)

    return agent


# ─────────────────────────────────────────
# Test cases
# ─────────────────────────────────────────

TEST_CASES = {

    # Case 1 — low/low/infrequent → notify + stop, no approvals
    "low_infrequent": ErrorEvent(
        service="auth-service",
        severity=Severity.LOW,
        impact=Impact.LOW,
        frequency=3,
        timing=15.0,
        message="Occasional 404 on /health endpoint",
    ),

    # Case 2 — low/low/frequent → notify + approval chain
    "low_frequent": ErrorEvent(
        service="auth-service",
        severity=Severity.LOW,
        impact=Impact.LOW,
        frequency=9,
        timing=2.0,
        message="Repeated 404 on /health endpoint — high frequency",
    ),

    # Case 3 — medium/medium → notify + email + approval chain
    "medium": ErrorEvent(
        service="payment-service",
        severity=Severity.MEDIUM,
        impact=Impact.MEDIUM,
        frequency=5,
        timing=4.0,
        message="Database connection pool exhausted",
    ),

    # Case 4 — high/high → urgent notify + email + approval chain
    "high": ErrorEvent(
        service="order-service",
        severity=Severity.HIGH,
        impact=Impact.HIGH,
        frequency=12,
        timing=1.5,
        message="Critical latency spike — p99 > 10s",
    ),

    # Case 5 — critical → urgent notify + email + phone log + approval chain
    "critical": ErrorEvent(
        service="checkout-service",
        severity=Severity.CRITICAL,
        impact=Impact.HIGH,
        frequency=20,
        timing=0.5,
        message="Service completely down — all requests failing with 500",
    ),
}


# ─────────────────────────────────────────
# Runner
# ─────────────────────────────────────────

async def run_case(name: str) -> None:
    event = TEST_CASES[name]
    logger.info("=" * 60)
    logger.info("Running test case: %s", name)
    logger.info("Service=%s  Severity=%s  Impact=%s  Freq=%s  Timing=%s",
                event.service, event.severity.value, event.impact.value,
                event.frequency, event.timing)
    logger.info("=" * 60)

    agent = build_agent()
    await agent.handle(event)
    logger.info("Test case '%s' complete.", name)


async def main() -> None:
    case = sys.argv[1] if len(sys.argv) > 1 else "medium"

    if case == "all":
        for name in TEST_CASES:
            await run_case(name)
            await asyncio.sleep(2)   # brief pause between cases
    elif case in TEST_CASES:
        await run_case(case)
    else:
        print(f"Unknown case '{case}'. Choose from: {', '.join(TEST_CASES)} or 'all'")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())