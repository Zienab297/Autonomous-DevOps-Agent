"""
Alert Agent - FastAPI Entry Point

Start with:
    uvicorn main:app --reload --port 8000

Expose locally with ngrok:
    ngrok http 8000
Then paste the https URL into Slack App → Interactivity → Request URL:
    https://xxxx.ngrok.io/slack/interactive
"""

import logging
import os

from fastapi import FastAPI
from dotenv import load_dotenv

from notifications.slack import SlackProvider
from notifications.email import EmailProvider
from approval             import ApprovalManager
from main.agent                import AlertAgent
from main.slack_webhook        import build_slack_router

# ─────────────────────────────────────────
# Logging
# ─────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────
# Load environment variables from .env
# ─────────────────────────────────────────
load_dotenv()

# ─────────────────────────────────────────
# Providers
# ─────────────────────────────────────────
slack = SlackProvider(
    bot_token=os.environ["SLACK_BOT_TOKEN"],
    channel=os.environ["SLACK_CHANNEL"],                        # e.g. #devops-alerts
    approval_channel=os.environ["SLACK_APPROVAL_CHANNEL"],      # e.g. #devops-approvals
)

email = EmailProvider(
    smtp_host=os.environ["SMTP_HOST"],
    smtp_port=int(os.environ["SMTP_PORT"]),
    username=os.environ["SMTP_USERNAME"],
    password=os.environ["SMTP_PASSWORD"],
    from_address=os.environ["EMAIL_FROM"],
    to_address=os.environ["EMAIL_TO"],
    approval_base_url=os.environ.get("APPROVAL_BASE_URL", ""),  # your ngrok/domain URL
)

# ─────────────────────────────────────────
# Approval Manager
# ─────────────────────────────────────────
approval_manager = ApprovalManager(
    timeout_seconds=int(os.environ.get("APPROVAL_TIMEOUT_SECONDS", 300)),
    team_channel=os.environ.get("SLACK_TEAM_CHANNEL", "#devops-team"),
    lead_channel=os.environ.get("SLACK_LEAD_CHANNEL", "#devops-leads"),
)

# ─────────────────────────────────────────
# Alert Agent
# ─────────────────────────────────────────
agent = AlertAgent(slack, email, approval_manager)

# ── Inject RAG handler ─────────────────────────────────────────────────────
# Replace this with your real RAG callable
async def rag_handler(event):
    from alert_agent.models import Solution
    logger.info("RAG handler called for service=%s", event.service)
    return Solution(
        possible_cause="Placeholder — replace with real RAG logic",
        recommended_fix="Placeholder — replace with real RAG logic",
        confidence=0.0,
        fix_commands=[],
    )

agent.set_rag_handler(rag_handler)

# ── Inject Self-Healing prepare handler ───────────────────────────────────
# Replace this with your real Self-Healing prepare callable
async def self_healing_prepare_handler(event, solution):
    logger.info("Self-Healing prepare called for service=%s", event.service)
    return []   # Return real list[FileModification]

agent.set_self_healing_prepare_handler(self_healing_prepare_handler)

# ── Inject Self-Healing apply handler ─────────────────────────────────────
# Replace this with your real Self-Healing apply callable
async def self_healing_apply_handler(event, modifications):
    logger.info(
        "Self-Healing apply called for service=%s — %d file(s)",
        event.service, len(modifications),
    )

agent.set_self_healing_apply_handler(self_healing_apply_handler)

# ─────────────────────────────────────────
# FastAPI app
# ─────────────────────────────────────────
app = FastAPI(title="Alert Agent")

# Slack interactive webhook — handles Approve / Deny / Forward button clicks
app.include_router(build_slack_router(approval_manager, slack))


@app.get("/health")
def health():
    return {"status": "ok"}