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

# Required for Monitoring / Knowledge / Self-Healing agents
GROQ_API_KEY=your_groq_key_here

# Optional — email approvals and alerts
ALERT_SMTP_HOST=smtp.gmail.com
ALERT_SMTP_PORT=587
ALERT_SMTP_USERNAME=you@gmail.com
ALERT_SMTP_PASSWORD=xxxx xxxx xxxx xxxx
ALERT_FROM_ADDRESS=DevOps Agent <you@gmail.com>
ALERT_ENGINEER_EMAIL=you@company.com
ALERT_TEAM_EMAILS=teammate1@company.com,teammate2@company.com
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

The alert agent sends email notifications and handles approvals via clickable email links.
Approvals appear on both your terminal (CLI) and in email simultaneously — first response wins.

**Two routing lanes:**
- **Normal** (LOW / MEDIUM severity) → email goes to `ALERT_ENGINEER_EMAIL` only
- **Emergency** (HIGH / CRITICAL) → email goes to the full team (`ALERT_TEAM_EMAILS`) simultaneously

---

### Step 1 — Configure Gmail

1. Google Account → **Security** → enable **2-Step Verification**
2. Go to **App Passwords** → generate one for "Mail" → copy the 16-character password

Add to `.env`:

```env
ALERT_SMTP_HOST=smtp.gmail.com
ALERT_SMTP_PORT=587
ALERT_SMTP_USERNAME=you@gmail.com
ALERT_SMTP_PASSWORD=xxxx xxxx xxxx xxxx
ALERT_FROM_ADDRESS=DevOps Agent <you@gmail.com>
ALERT_ENGINEER_EMAIL=lead@company.com
ALERT_TEAM_EMAILS=alice@company.com,bob@company.com,carol@company.com
```

> `ALERT_TEAM_EMAILS` is a comma-separated list. Leave it blank to disable team broadcasts.

---

### Step 2 — Install Cloudflare Tunnel (Windows)

The approval server needs a public URL so email links work from any device.
We use **cloudflared** (free, no account required).

**Install (one-time):**

1. Download the Windows binary:
   ```
   https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe
   ```
2. Rename to `cloudflared.exe`
3. Move it to `C:\Windows\System32\` so it's available from any terminal

**Verify it works:**
```bash
cloudflared --version
```
You should see output like `cloudflared version 2024.x.x`.

**Linux / macOS:**
```bash
# macOS
brew install cloudflared

# Linux (Debian/Ubuntu)
wget https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
sudo dpkg -i cloudflared-linux-amd64.deb
```

---

### Step 3 — Run

No separate server needed. The approval server starts automatically when you run `devops`.
At startup you'll see:

```
  [Email] approvals/alerts → lead@company.com  |  emergency team → 3 address(es)
  [ApprovalServer] 🌐 Public URL: https://xxxx.trycloudflare.com
```

When an approval gate is reached, an email is sent to the engineer with **✅ Approve** and
**❌ Deny** buttons. You can also type `yes` or `no` directly in the terminal — first response wins.

---

### Dashboard — alerting_agent status

When email is configured, the dashboard shows:

```
● alerting_agent           IDLE (email)
```

When email is not configured:

```
○ alerting_agent           —
```

---

### No Cloudflared?

If `cloudflared` is not installed, the approval server falls back to `localhost` only —
email links will only work if you open them on the **same machine** running the agent.
CLI approval always works regardless.

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