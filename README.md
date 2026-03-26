# DevOps Multi-Agent System

Autonomous DevOps pipeline — scans your project, generates DevOps files, pushes to GitHub, monitors CI/CD, detects incidents, investigates with RAG, and applies self-healing fixes — all with engineer approval at every step.

---

## What It Does

```
devops  (run from any project folder)
   │
   ├── 1. ScaffoldAgent    → generates Dockerfile, docker-compose, GitHub Actions, k8s manifests
   ├── 2. CI/CD Agent      → pushes to GitHub, monitors Actions pipeline
   ├── 3. MonitoringAgent  → analyzes CI/CD logs, detects anomalies
   ├── 4. KnowledgeAgent   → investigates incident using RAG + web search
   └── 5. SelfHealingAgent → applies fix to files
```

Every step asks for your approval before continuing.

---

## Prerequisites

Before running anything, make sure you have these installed:

| Tool | Purpose | Install |
|------|---------|---------|
| Python 3.11+ | Runtime | https://python.org |
| Conda | Environment manager | https://docs.conda.io |
| Docker Desktop | Run Qdrant | https://docker.com |
| Ollama | Local LLM for scaffold + knowledge | https://ollama.com |
| Git | Push to GitHub | https://git-scm.com |

---

## One-Time Setup

### 1. Create and activate environment

```bash
conda create -n auto_devops python=3.11 -y
conda activate auto_devops
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Pull the Ollama model

```bash
ollama pull llama3.2:3b
```

### 4. Configure environment variables

```bash
cp .env.template .env
```

Edit `.env` and fill in:

```env
# Required for CI/CD Agent to push and monitor GitHub Actions
GITHUB_TOKEN=ghp_your_token_here

# Required for SelfHealingAgent to fix files
GROQ_API_KEY=your_groq_key_here

# Optional — for Slack/email alerts
SLACK_BOT_TOKEN=xoxb-...
SMTP_USERNAME=you@gmail.com
SMTP_PASSWORD=your-app-password
```

> **GITHUB_TOKEN**: Go to GitHub → Settings → Developer settings → Personal access tokens → Generate new token → select `repo` and `workflow` scopes.

> **GROQ_API_KEY**: Get a free key at https://console.groq.com

### 5. Install the CLI tool

```bash
pip install -e .
```

This lets you run `devops` from any folder on your machine.

---

## Every Time You Run

You need **3 services running** before you type `devops`:

### Terminal 1 — Start Ollama
```bash
ollama serve
```
Keep this terminal open.

### Terminal 2 — Start Qdrant
```bash
docker run -d -p 6333:6333 --name qdrant qdrant/qdrant
```
This runs in the background. You only need to do this once — next time just check it's running:
```bash
docker start qdrant
```

### Terminal 3 — Run the agent
```bash
cd /path/to/your/project
conda activate auto_devops
devops
```

---

## Verify Everything Is Working

Before running `devops`, check all services are up:

```bash
# Qdrant running?
curl http://localhost:6333

# Ollama running?
curl http://localhost:11434

# Docker running?
docker ps
```

If you see responses from all three, you're good to go.

---

## Dashboard

When you run `devops`, you'll see a live dashboard:

```
AGENTS
● scaffold_agent           IDLE    ← registered and ready
● cicd_agent               IDLE
● monitoring_agent         IDLE
● knowledge_agent          IDLE    ← this is what we fixed
● self_healing_agent       IDLE
○ alerting_agent           —       ← optional, not configured
```

All 5 agents should show `●` (green dot). If any shows `○` (empty), check the error in the logs above the dashboard.

---

## Common Errors and Fixes

### `No module named 'qdrant_client'`
```bash
pip install qdrant-client
```

### `Failed to register KnowledgeAgentAdapter: ... qdrant_client`
Qdrant server is not running. Start it:
```bash
docker start qdrant
# or if first time:
docker run -d -p 6333:6333 --name qdrant qdrant/qdrant
```

### `No module named 'sentence_transformers'`
```bash
pip install sentence-transformers
```

### `No module named 'ddgs'` or `No module named 'duckduckgo_search'`
```bash
pip install duckduckgo-search
```

### `ollama serve` gives `address already in use`
Ollama is already running in the background — this is fine, ignore it.

### `knowledge_agent` shows `○` (not registered)
Check the log line above the dashboard — it will say exactly why. Most common causes:
- Qdrant not running → `docker start qdrant`
- Ollama not running → `ollama serve`
- Missing package → `pip install -r requirements.txt`

### `GITHUB_TOKEN not set`
Add your token to `.env`:
```env
GITHUB_TOKEN=ghp_your_token_here
```

---

## Running Individual Agents (for testing)

### Knowledge Agent only
```bash
docker start qdrant
ollama serve
cd agents/knowledge_agent
python Test_knowledge_agent.py
```

### Scaffold Agent only
```bash
ollama serve
cd agents/scaffold_agent
python test_scaffold_agent.py
```

### Core pipeline test
```bash
docker start qdrant
cd core/test
python test_core.py
```

### Integration test
```bash
pytest tests/cli_core_integration_test.py -v
```

---

## Alert Agent (Optional)

The alert agent sends Slack/email notifications and handles approvals via Slack buttons.

### Setup Slack App

1. Go to https://api.slack.com/apps → **Create New App** → **From Scratch**
2. Under **OAuth & Permissions** add Bot Token Scopes: `chat:write`, `chat:write.public`, `channels:history`, `channels:read`, `groups:history`
3. Install to workspace, copy the **Bot Token** (`xoxb-...`)
4. Create channels: `#devops-alerts`, `#devops-approvals`
5. Invite bot to both channels: `/invite @YourBotName`

### Enable Interactivity

Start ngrok (installed with requirements):
```bash
python agents/alert_agent/main/ngrok.py
```

Copy the URL (e.g. `https://xxxx.ngrok-free.app`), then in Slack App settings:
- **Interactivity & Shortcuts** → ON → Request URL: `https://xxxx.ngrok-free.app/slack/interactive`

### Setup Gmail

1. Google Account → Security → 2-Step Verification → enable
2. App Passwords → create one for "Mail" → copy the 16-character password

### Run the Alert Server

```bash
cd agents/alert_agent/
uvicorn app:app --reload --port 8000
```

### Test Alerts

```bash
python agents/alert_agent/test_agent.py low_infrequent   # Slack only
python agents/alert_agent/test_agent.py medium           # Slack + Email
python agents/alert_agent/test_agent.py critical         # Full chain
python agents/alert_agent/test_agent.py all              # Run all cases
```

---

## Updating the CLI

If you change the agent code:

```bash
pip uninstall devops-agent -y
pip install -e .
```

---

## Project Structure

```
Autonomous-DevOps-Agent/
├── devops_agent/           ← CLI entry point (the `devops` command)
├── core/                   ← Orchestrator, event bus, state manager
├── agents/
│   ├── scaffold_agent/     ← Generates Dockerfile, CI/CD, k8s files
│   ├── cicd_agent/         ← Monitors GitHub Actions
│   ├── monitoring_agent/   ← Detects anomalies in logs
│   ├── knowledge_agent/    ← RAG + web search investigation
│   ├── self_healing_agent/ ← Applies file fixes
│   └── alert_agent/        ← Slack + email notifications
├── providers/              ← GitHub Actions provider
├── .env.template           ← Copy this to .env
└── requirements.txt
```