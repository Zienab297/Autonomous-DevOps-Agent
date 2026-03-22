# DevOps-Multi-Agent-System

# Alert Agent

Autonomous DevOps incident pipeline — detects errors, investigates with RAG, proposes file fixes, and applies them only after engineer approval.

---

## Prerequisites

- Python 3.11+
- Conda environment activated
- ngrok installed
- A Slack workspace where you can create apps
- A Gmail account (or any SMTP provider)

---

## Step 1 — Install Dependencies

```bash
conda activate auto_devops
pip install -r requirements.txt
```

---

## Step 2 — Create Your Slack App

1. Go to https://api.slack.com/apps → **Create New App** → **From Scratch**
2. Give it a name (e.g. `Alert Agent`) and pick your workspace
3. Go to **OAuth & Permissions** → under **Bot Token Scopes** add:
   - `chat:write`
   - `chat:write.public`
   - `channels:history`
   - `channels:read`
   - `groups:history`
   
4. Click **Install to Workspace** → copy the **Bot Token** (`xoxb-...`)
5. Create two channels in Slack:
   - `#devops-alerts` — for normal alerts
   - `#devops-approvals` — for approval requests
6. Invite the bot to both channels: `/invite @YourBotName`

---

## Step 3 — Enable Slack Interactivity

> You need ngrok running before this step so Slack can verify the URL.

Start ngrok in a separate terminal:
```bash
ALready installed with req file, just run ngrok.py file in main and will get the url in terminal 
```
Copy the URL it gives you (e.g. `https://xxxx.ngrok-free.app`)

Then in your Slack App settings:
1. Go to **Interactivity & Shortcuts** → toggle **ON**
2. Set Request URL to:
   ```
   https://xxxx.ngrok-free.app/slack/interactive
   ```
3. Click **Save Changes**
4. Go to **Install App** → **Reinstall to Workspace**

---

## Step 4 — Set Up Gmail

1. Go to your Google Account → **Security** → **2-Step Verification** → enable it
2. Then go to **App Passwords** → create one for "Mail"
3. Copy the 16-character password — this is your `SMTP_PASSWORD`

---

## Step 5 — Configure .env

Copy the template and fill in your values:
```bash
cp .env.template .env
```

Edit `.env`:
```env
# Slack
SLACK_BOT_TOKEN=xoxb-your-token-here
SLACK_CHANNEL=#devops-alerts
SLACK_APPROVAL_CHANNEL=#devops-approvals
SLACK_TEAM_CHANNEL=#devops-team
SLACK_LEAD_CHANNEL=#devops-leads

# Email
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=you@gmail.com
SMTP_PASSWORD=your-16-char-app-password
EMAIL_FROM=you@gmail.com
EMAIL_TO=engineer@gmail.com

# Approval
APPROVAL_BASE_URL=https://xxxx.ngrok-free.app
APPROVAL_TIMEOUT_SECONDS=300
```

---

## Step 6 — Run the Server

```bash
cd agents/alert_agent/
uvicorn app:app --reload --port 8000
```

You should see:
```
INFO: Uvicorn running on http://127.0.0.1:8000
```

---

## Step 7 — Test the Agent

Run a test case:
```bash
# Low/low/infrequent → notify Slack only, pipeline stops
python test_agent.py low_infrequent

# Low/low/frequent → full approval chain
python test_agent.py low_frequent

# Medium → Slack + Email + approvals
python test_agent.py medium

# High → urgent Slack + Email + approvals
python test_agent.py high

# Critical → urgent Slack + Email + phone log + approvals
python test_agent.py critical

# Run all cases sequentially
python test_agent.py all
```

---

## Approval Flow

When an approval message appears in Slack:

| Button | Result |
|---|---|
| ✅ Approve | Pipeline continues to next step |
| ❌ Deny | Pipeline stops, engineer notified |
| → Forward to Team | Sent to `#devops-team` for approval |
| → Forward to Lead | Sent to `#devops-leads` for approval |

Approvals time out after `APPROVAL_TIMEOUT_SECONDS` (default 300s) — treated as Deny.

## Project Structure

```
agents/alert_agent/
├── main/
│   ├── agent.py             # Main orchestrator
│   ├── slack_webhook.py     # Slack button click handler
│   ├── approval.py          # Approval lifecycle manager
│   ├── models.py            # Data models
│   ├── routing.py           # Routing rules engine
│   └── notifications/
│       ├── slack.py         # Slack provider
│       └── email.py         # Email provider
├── app.py                    # FastAPI entry point
├── .env                     # Your config (never commit this)
├── .env.template            # Config template
├── requirements.txt
└── test_agent.py            # Test script
```

---

## Common Issues

**`ModuleNotFoundError`** — make sure your conda env is activated and you ran `pip install -r requirements.txt` inside it.

**Slack buttons not working** — check that ngrok is running and the Request URL in Slack Interactivity settings matches your current ngrok URL. Free tier URLs change on every restart.

**Emails not sending** — Gmail requires an App Password, not your real password. Make sure 2-Step Verification is enabled first.

**Approval times out immediately** — check `APPROVAL_TIMEOUT_SECONDS` in your `.env`, default is 300 seconds.

## SDK and CLI Setting up Commands 

setting up the devops agent to run at any directory
```bash
pip install -e .
```

when updating the agent use both command
```bash
pip uninstall devops-agent -y
pip install -e .
```

testing the integration between devops_agent and the core
```bash
pytest tests/cli_core_integration_test.py -v
```


## Running the Agents

**Knowledge Agent** — make sure Qdrant is running first
```bash
docker run -p 6333:6333 qdrant/qdrant
cd agents/knowledge_agent
python test_knowledge_agent.py
```

**Core** — make sure Qdrant is running first
```bash
docker run -p 6333:6333 qdrant/qdrant
cd core/test
python test_core.py
```
**Scaffold Agent** — make sure Ollama is running first
```bash
ollama pull llama3.2:3b
cd agents/scaffold_agent
python test_scaffold_agent.py
```
