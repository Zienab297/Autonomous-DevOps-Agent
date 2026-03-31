"""
core/orchestrator.py

Incident flow (Monitor → Knowledge → Self-Healing):
----------------------------------------------------
1. MonitoringAgent detects anomaly, publishes INCIDENT_CREATED
   event.data includes:
       - incident_id, service, severity, description
       - files_to_fix: [{file, line, function, exception, fix_description}, ...]
         ← populated from CI/CD log tracebacks by the log parser
       - issue_type: "syntax" | "import" | "runtime" | "unknown"
         ← used to route between auto-fix and user instructions

2. _on_incident_created:
       - checks issue_type from event.data
       - if "syntax": routes to SelfHealingAgent directly (auto-fix)
       - if other: prints user-facing instructions and calls KnowledgeAgent
       - builds Solution, attaches FileToFix objects from files_to_fix
       - publishes INVESTIGATION_COMPLETE(solution)

3. _on_investigation_complete:
       - calls self_healing_agent.remediate(solution) → SelfHealingResult
         (solution already carries files_to_modify, no extra passing needed)
       - publishes REMEDIATION_COMPLETE or REMEDIATION_FAILED based on result

4. _on_remediation_complete / _on_remediation_failed:
       - updates incident status → RESOLVED or FAILED
       - sends alert (when alerting agent is registered)
"""

import asyncio
import logging
import sys
import pathlib
from datetime import datetime
from typing import Optional

from core.models import (
    AgentStatus,
    Incident,
    IncidentStatus,
    RemediationStatus,
    Solution,
)
from core.event_bus import EventBus, Event, EventType
from core.state_manager import StateManager
from core.context_manager import ContextManager
from core.agent_registery import AgentRegistry
from core.approval_manager import ApprovalManager
from core.approval_server  import ApprovalServer

try:
    from core.email_client import EmailClient as _EmailClient
except ImportError:
    _EmailClient = None

# FileToFix lives in self_healing/models.py — imported here to convert
# monitoring dicts → typed objects before passing to self-healing agent
from agents.self_healing_agent.models import FileToFix, Solution as SHSolution
from agents.monitoring_agent.agent import MonitoringAgent
from agents.monitoring_agent.config import MonitoringConfig

# SelfHealingAgent registered automatically so the orchestrator is self-contained
from agents.self_healing_agent.self_healing_agent import SelfHealingAgent

logger = logging.getLogger(__name__)

# ── ANSI colour helpers ────────────────────────────────────────────────────────
_R  = "\033[0m"
_B  = "\033[1m"
_CY = "\033[36m"
_YL = "\033[33m"
_RD = "\033[31m"
_GR = "\033[32m"
_DM = "\033[2m"


def _box(lines: list[str], colour: str = _CY) -> str:
    """Render a simple box around a list of strings."""
    width = max(len(l) for l in lines) + 4
    top   = f"  {colour}┌{'─' * width}┐{_R}"
    bot   = f"  {colour}└{'─' * width}┘{_R}"
    body  = [f"  {colour}│{_R}  {l:<{width - 2}}{colour}│{_R}" for l in lines]
    return "\n".join([top, *body, bot])


def step(label: str, value: str) -> None:
    """Print a simple labeled step output."""
    print(f"  {_B}{label}{_R}: {value}")


class Orchestrator:

    def __init__(self, email=None):
        self.event_bus       = EventBus()
        self.state_manager   = StateManager()
        self.context_manager = ContextManager()
        self.registry        = AgentRegistry()
        self._email          = email

        self.approval = ApprovalManager(
            email           = email,
            timeout_seconds = int(__import__('os').getenv("APPROVAL_TIMEOUT_SECONDS", "300")),
            registry        = self.registry,
        )

        # HTTP server for email approve/deny link callbacks.
        # Started/stopped by devops.py around the pipeline run.
        self._approval_server: Optional[ApprovalServer] = (
            ApprovalServer(approval_manager=self.approval, email_client=email)
            if email else None
        )

        self._running = False

        self._subscribe_to_events()
        self._register_monitoring_agent()
        self._register_knowledge_agent()
        self._register_self_healing_agent()   # ← NEW: always registered

        # ── Dashboard state ───────────────────────────────────────────────────
        self._dashboard: dict = {
            "started_at"  : datetime.utcnow(),
            "stage"       : "idle",
            "project"     : "",
            "repo_url"    : "",
            "cicd_status" : "",
            "incidents"   : [],
            "agents"      : {},
            "last_event"  : "",
        }

        logger.info("Orchestrator initialized")

    def _subscribe_to_events(self) -> None:
        self.event_bus.subscribe(EventType.SCAFFOLD_STARTED,       self._on_scaffold_started)
        self.event_bus.subscribe(EventType.SCAFFOLD_COMPLETE,      self._on_scaffold_complete)
        self.event_bus.subscribe(EventType.SCAFFOLD_FAILED,        self._on_scaffold_failed)
        self.event_bus.subscribe(EventType.INCIDENT_CREATED,       self._on_incident_created)
        self.event_bus.subscribe(EventType.INVESTIGATION_COMPLETE, self._on_investigation_complete)
        self.event_bus.subscribe(EventType.REMEDIATION_COMPLETE,   self._on_remediation_complete)
        self.event_bus.subscribe(EventType.REMEDIATION_FAILED,     self._on_remediation_failed)

    def register_agent(self, name: str, agent: object, metadata: Optional[dict] = None) -> None:
        self.registry.register(name, agent, metadata)
        self.state_manager.set_agent_status(name, AgentStatus.IDLE)
        logger.info(f"[Orchestrator] Agent registered: '{name}'")

    # ── Auto-registration helpers ─────────────────────────────────────────────

    def _register_monitoring_agent(self) -> None:
        """
        Instantiate and register the MonitoringAgent if not already registered.
        Starts in continuous file-backend mode so it always monitors.
        """
        if self.registry.get_agent("monitoring_agent"):
            return

        config = MonitoringConfig(
            collector_backend="file",
            log_dir="logs",
        )
        agent = MonitoringAgent(
            event_bus       = self.event_bus,
            registry        = self.registry,
            config          = config,
            context_manager = self.context_manager,
            state_manager   = self.state_manager,
        )
        self.registry.register("monitoring_agent", agent)
        self.state_manager.set_agent_status("monitoring_agent", AgentStatus.IDLE)
        logger.info("[Orchestrator] MonitoringAgent registered automatically")

    async def start_monitoring_agent(self) -> None:
        """
        Start the MonitoringAgent's background poll loop.
        Call once from your async entrypoint after ``await orchestrator.start()``.
        The monitoring agent will then run continuously until the process stops.
        """
        agent = self.registry.get_agent("monitoring_agent")
        if agent is None:
            logger.error("[Orchestrator] MonitoringAgent not found in registry")
            return
        if getattr(agent, "_poll_task", None) and not agent._poll_task.done():
            logger.debug("[Orchestrator] MonitoringAgent already running — skipping start")
            return
        await agent.start()
        logger.info("[Orchestrator] MonitoringAgent started — monitoring continuously")

    def _register_knowledge_agent(self) -> None:
        """
        Instantiate and register the KnowledgeAgentAdapter if not already registered.

        Root cause of 'cannot import AgentResponse from shared.models':
        Both scaffold_agent and knowledge_agent have a 'shared/' sub-package.
        Adding knowledge_agent's root to sys.path causes Python to resolve
        'shared.models' against whichever 'shared/' it finds first — which is
        scaffold_agent's if that was inserted earlier.

        Fix: never add knowledge_agent's root to sys.path. All imports already
        work via the full dotted path 'agents.knowledge_agent.*' from the
        project root which is already on sys.path.
        """
        if self.registry.get_agent("knowledge_agent"):
            return

        try:
            # ── Qdrant ingestion (idempotent) ──────────────────────────────
            # Uses fully-qualified package paths — no sys.path manipulation needed.
            try:
                from agents.knowledge_agent.shared.config import load_config as _ka_cfg
                from agents.knowledge_agent.ingestion.pipeline import run_pipeline
                from qdrant_client import QdrantClient

                _cfg    = _ka_cfg()
                _client = QdrantClient(host=_cfg.qdrant_host, port=_cfg.qdrant_port)
                _needs  = True
                try:
                    if _client.count(collection_name=_cfg.collection_name).count > 0:
                        _needs = False
                        logger.info("[Orchestrator] Qdrant already populated — skipping ingestion")
                except Exception:
                    pass
                if _needs:
                    logger.info("[Orchestrator] Populating Qdrant knowledge base (first run)...")
                    run_pipeline()
                    logger.info("[Orchestrator] Qdrant ingestion complete")
            except Exception as ie:
                logger.warning(
                    "[Orchestrator] Knowledge base ingestion skipped: %s "
                    "— queries will fall back to LLM + web search", ie
                )

            from agents.knowledge_agent.knowledge_core.knowledge_agent_adapter import KnowledgeAgentAdapter
            agent = KnowledgeAgentAdapter()
            self.registry.register("knowledge_agent", agent)
            self.state_manager.set_agent_status("knowledge_agent", AgentStatus.IDLE)
            logger.info("[Orchestrator] KnowledgeAgentAdapter registered successfully")

        except Exception as exc:
            logger.error(
                "[Orchestrator] Failed to register KnowledgeAgentAdapter: %s — "
                "check Qdrant (localhost:6333) and Ollama (localhost:11434) are running", exc
            )

    def _register_self_healing_agent(self) -> None:
        """
        Instantiate and register the SelfHealingAgent if not already registered.
        apply_changes=True so it actually writes fixes to disk.
        """
        if self.registry.get_agent("self_healing_agent"):
            return

        agent = SelfHealingAgent(
            event_bus    = self.event_bus,
            registry     = self.registry,
            apply_changes= True,
            project_root = ".",
        )
        self.registry.register("self_healing_agent", agent)
        self.state_manager.set_agent_status("self_healing_agent", AgentStatus.IDLE)
        logger.info("[Orchestrator] SelfHealingAgent registered automatically (apply_changes=True)")

    # ── Dashboard ─────────────────────────────────────────────────────────────

    _R  = "\033[0m"
    _B  = "\033[1m"
    _DM = "\033[2m"
    _CY = "\033[36m"
    _GR = "\033[32m"
    _YL = "\033[33m"
    _RD = "\033[31m"
    _WH = "\033[97m"

    _SEV_COL = {
        "critical": "\033[31m", "high": "\033[31m",
        "medium":   "\033[33m", "low":  "\033[32m",
    }

    def _dash(self, key: str, value) -> None:
        self._dashboard[key] = value

    def _track_incident(self, incident_id: str, service: str, severity: str,
                        description: str, status: str = "OPEN") -> None:
        for rec in self._dashboard["incidents"]:
            if rec["id"] == incident_id:
                rec["status"]   = status
                rec["severity"] = severity
                return
        self._dashboard["incidents"].append({
            "id"         : incident_id,
            "service"    : service,
            "severity"   : severity,
            "description": description,
            "status"     : status,
            "at"         : datetime.utcnow().strftime("%H:%M:%S"),
        })

    def print_dashboard(self, event_line: str = "") -> None:
        B, R, DM, CY, GR, YL, RD, WH = (
            self._B, self._R, self._DM, self._CY,
            self._GR, self._YL, self._RD, self._WH,
        )
        W = 66

        uptime_s = int((datetime.utcnow() - self._dashboard["started_at"]).total_seconds())
        um, us   = divmod(uptime_s, 60)
        uh, um   = divmod(um, 60)
        uptime   = f"{uh:02d}:{um:02d}:{us:02d}"

        stage_col = {
            "idle": DM, "scaffold": CY, "cicd": CY,
            "monitoring": YL, "incident": RD, "healing": YL, "done": GR,
        }.get(self._dashboard["stage"], WH)

        lines = []
        sep = lambda c="─": f"  {c * W}"

        lines.append(f"\n{B}{CY}  {'═' * W}{R}")
        lines.append(f"{B}{CY}  {'AUTONOMOUS DEVOPS AGENT':^{W}}{R}")
        lines.append(f"{B}{CY}  {'═' * W}{R}")

        lines.append(
            f"  {B}Stage   {R}: {stage_col}{self._dashboard['stage'].upper():<12}{R}"
            f"  {DM}uptime {uptime}{R}"
        )
        if self._dashboard["project"]:
            lines.append(f"  {B}Project {R}: {self._dashboard['project']}")
        if self._dashboard["repo_url"]:
            lines.append(f"  {B}Repo    {R}: {self._dashboard['repo_url']}")
        if self._dashboard["cicd_status"]:
            col = GR if "success" in self._dashboard["cicd_status"] else RD if "fail" in self._dashboard["cicd_status"] else YL
            lines.append(f"  {B}CI/CD   {R}: {col}{self._dashboard['cicd_status']}{R}")

        lines.append(sep())
        lines.append(f"  {B}AGENTS{R}")
        all_agents = [
            "scaffold_agent", "cicd_agent", "monitoring_agent",
            "knowledge_agent", "self_healing_agent", "alerting_agent",
        ]
        for name in all_agents:
            registered = self.registry.get_agent(name) is not None
            raw_status = self._dashboard["agents"].get(name, "IDLE" if registered else "—")
            if not registered:
                col, sym = DM, "○"
            elif raw_status in ("RUNNING",):
                col, sym = YL, "▶"
            elif raw_status in ("IDLE",):
                col, sym = GR, "●"
            else:
                col, sym = DM, "○"
            lines.append(
                f"  {sym} {name:<24} {col}{raw_status}{R}"
            )

        lines.append(sep())
        lines.append(f"  {B}INCIDENTS ({len(self._dashboard['incidents'])}){R}")
        if not self._dashboard["incidents"]:
            lines.append(f"  {GR}  No incidents{R}")
        else:
            for inc in self._dashboard["incidents"][-5:]:
                sev    = inc["severity"].lower()
                sc     = self._SEV_COL.get(sev, WH)
                st     = inc["status"]
                st_col = GR if st == "RESOLVED" else RD if st == "FAILED" else YL
                lines.append(
                    f"  {DM}{inc['at']}{R}  "
                    f"{sc}{B}[{inc['severity'].upper():<8}]{R}  "
                    f"{inc['service']:<20}  "
                    f"{st_col}{st:<14}{R}  "
                    f"{DM}{inc['description'][:35]}{R}"
                )

        if event_line or self._dashboard["last_event"]:
            msg = event_line or self._dashboard["last_event"]
            lines.append(sep())
            lines.append(f"  {B}LAST EVENT{R}")
            lines.append(f"  {DM}{msg}{R}")

        lines.append(f"  {B}{CY}{'═' * W}{R}\n")
        print("\n".join(lines))
        if event_line:
            self._dash("last_event", event_line)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._running = True
        logger.info("[Orchestrator] Started")

    async def stop(self) -> None:
        self._running = False
        for record in self.registry.get_all():
            self.state_manager.set_agent_status(record.name, AgentStatus.STOPPED)
        logger.info("[Orchestrator] Stopped")

    # ── Scaffold ──────────────────────────────────────────────────────────────

    async def run_scaffold(
        self,
        project_path  : str,
        dry_run       : bool = False,
        skip_scaffold : bool = False,
    ) -> None:
        logger.info(f"[Orchestrator] run_scaffold: {project_path} skip_scaffold={skip_scaffold}")
        await self.event_bus.publish(Event(
            type=EventType.SCAFFOLD_STARTED,
            source="cli",
            data={
                "project_path" : project_path,
                "dry_run"      : dry_run,
                "skip_scaffold": skip_scaffold,
            },
        ))

    async def _on_scaffold_started(self, event: Event) -> None:
        self._dash("stage",   "scaffold")
        self._dash("project", event.data.get("project_path", ""))
        logger.info("[Orchestrator] SCAFFOLD_STARTED → ScaffoldAgent")

        project_path   = event.data.get("project_path")
        dry_run        = event.data.get("dry_run", False)
        skip_scaffold  = event.data.get("skip_scaffold", False)

        if skip_scaffold:
            logger.info("[Orchestrator] skip_scaffold=True — skipping file generation")
            self.print_dashboard("Scaffold skipped — using existing files")
            await self.event_bus.publish(Event(
                type=EventType.SCAFFOLD_COMPLETE,
                source="orchestrator",
                data={
                    "project_path"   : project_path,
                    "dry_run"        : dry_run,
                    "language"       : "unknown",
                    "framework"      : "existing",
                    "generated_files": [],
                },
            ))
            return

        self.print_dashboard("ScaffoldAgent started")

        scaffold_agent = self.registry.get_agent("scaffold_agent")
        if not scaffold_agent:
            logger.error("[Orchestrator] ScaffoldAgent not registered!")
            await self.event_bus.publish(Event(
                type=EventType.SCAFFOLD_FAILED,
                source="orchestrator",
                data={"error": "ScaffoldAgent not registered"},
            ))
            return

        self.state_manager.set_agent_status("scaffold_agent", AgentStatus.RUNNING)
        self._dashboard["agents"]["scaffold_agent"] = "RUNNING"

        try:
            result = scaffold_agent.run(project_path, dry_run=dry_run)
            await self.event_bus.publish(Event(
                type=EventType.SCAFFOLD_COMPLETE,
                source="scaffold_agent",
                data={
                    "project_path"   : project_path,
                    "dry_run"        : dry_run,
                    "language"       : result.language.value,
                    "framework"      : result.framework.value,
                    "generated_files": [f.filename for f in result.generated_files],
                },
            ))
            self.print_dashboard(f"Scaffold complete — {result.framework.value} ({result.language.value}), {len(result.generated_files)} files generated")
        except Exception as e:
            logger.error(f"[Orchestrator] ScaffoldAgent failed: {e}")
            self.print_dashboard(f"Scaffold FAILED: {e}")
            await self.event_bus.publish(Event(
                type=EventType.SCAFFOLD_FAILED,
                source="scaffold_agent",
                data={"error": str(e), "project_path": project_path},
            ))
        finally:
            self.state_manager.set_agent_status("scaffold_agent", AgentStatus.IDLE)
            self._dashboard["agents"]["scaffold_agent"] = "IDLE"

    async def _on_scaffold_complete(self, event: Event) -> None:
        files        = event.data.get("generated_files", [])
        project_path = event.data.get("project_path")
        dry_run      = event.data.get("dry_run", False)
        language     = event.data.get("language", "")
        framework    = event.data.get("framework", "")

        logger.info(f"[Orchestrator] SCAFFOLD_COMPLETE ({len(files)} files)")

        if dry_run:
            self._dash("stage", "done")
            self.print_dashboard("Dry-run complete — no CI/CD triggered")
            return

        approved = await self.approval.request_approval(
            title=f"Scaffold complete — {framework} ({language}). Proceed to CI/CD?",
            details=files,
            context={"project_path": project_path},
        )
        if not approved:
            logger.info("[Orchestrator] CI/CD cancelled by developer.")
            print("\n  Pipeline stopped. No CI/CD triggered.\n")
            return

        repo_url = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: input("\n  GitHub repo URL (https://github.com/user/repo): ").strip()
        )
        if not repo_url:
            print("  No repo URL — stopping.\n")
            return

        import os
        token = os.getenv("GITHUB_TOKEN", "")
        if not token:
            print("  No GITHUB_TOKEN in .env — stopping.\n")
            return
        print("  [OK]  GITHUB_TOKEN loaded from .env")

        pushed = await self._push_to_github(
            project_path=project_path,
            repo_url=repo_url,
            token=token,
        )
        if not pushed:
            self.print_dashboard("Git push failed — stopping")
            return

        self._dash("repo_url", repo_url)

        cicd_agent = self.registry.get_agent("cicd_agent")
        if not cicd_agent:
            logger.info("[Orchestrator] CI/CD Agent not registered — pipeline running on GitHub")
            self._dash("stage", "cicd")
            self._dash("cicd_status", "triggered (no log collection)")
            self.print_dashboard("GitHub Actions triggered by push — register CI/CD Agent to collect logs")
            await self.event_bus.publish(Event(
                type=EventType.DEPLOYMENT_COMPLETE,
                source="orchestrator",
                data={"project_path": project_path, "logs": [], "repo_url": repo_url},
            ))
            return

        self._dash("stage", "cicd")
        self._dash("cicd_status", "running")
        self._dashboard["agents"]["cicd_agent"] = "RUNNING"
        self.state_manager.set_agent_status("cicd_agent", AgentStatus.RUNNING)
        self.print_dashboard("CI/CD Agent collecting logs from GitHub Actions")

        try:
            repo = repo_url.replace("https://github.com/", "").replace(".git", "")

            # GitHub Actions can take 30-90s to register a new run after a push.
            # We poll every 10s for up to 2 minutes before giving up.
            print("  Waiting for GitHub Actions run to appear (up to 2 min)...")
            run = None
            for _attempt in range(12):          # 12 × 10s = 120s max
                await asyncio.sleep(10)
                self.print_dashboard(f"Waiting for GitHub Actions run… ({(_attempt+1)*10}s)")
                run = await self._get_latest_run(cicd_agent, repo, token)
                if run:
                    break

            if not run:
                self._dash("cicd_status", "no run found")
                self.print_dashboard(
                    "No GitHub Actions run found after 2 min — "
                    "check repo URL, GITHUB_TOKEN, and that a workflow .yml exists in .github/workflows/"
                )
                return

            run_id = run["id"]
            status = run["status"]
            logger.info(f"[Orchestrator] GitHub Actions Run ID: {run_id}")
            step("Run ID", str(run_id))

            while status in ("queued", "in_progress"):
                await asyncio.sleep(15)
                run    = await self._get_run(cicd_agent, repo, run_id, token)
                status = run.get("status", "unknown")
                concl  = run.get("conclusion", "")
                self._dash("cicd_status", f"{status} ({concl})" if concl else status)
                self.print_dashboard(f"CI/CD: {status}")

            conclusion = run.get("conclusion", "unknown")
            self._dash("cicd_status", conclusion)

            logs = await self._get_run_logs(cicd_agent, repo, run_id, token)
            self.print_dashboard(f"CI/CD {conclusion} — {len(logs)} log lines collected")

            await self.event_bus.publish(Event(
                type=EventType.DEPLOYMENT_COMPLETE,
                source="cicd_agent",
                data={
                    "project_path": project_path,
                    "repo_url"    : repo_url,
                    "run_id"      : run_id,
                    "conclusion"  : conclusion,
                    "logs"        : logs,
                },
            ))
        except Exception as e:
            logger.error(f"[Orchestrator] CI/CD Agent error: {e}", exc_info=True)
            self.print_dashboard(f"CI/CD error: {e}")
        finally:
            self._dashboard["agents"]["cicd_agent"] = "IDLE"
            self.state_manager.set_agent_status("cicd_agent", AgentStatus.IDLE)

    async def _on_scaffold_failed(self, event: Event) -> None:
        error = event.data.get("error", "unknown")
        self._dash("stage", "done")
        self.print_dashboard(f"Scaffold FAILED: {error}")

    # ── CI/CD helpers (unchanged) ─────────────────────────────────────────────

    async def _get_latest_run(self, cicd_agent, repo: str, token: str):
        import aiohttp
        headers = {
            "Authorization"       : f"Bearer {token}",
            "Accept"              : "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        url = f"https://api.github.com/repos/{repo}/actions/runs?per_page=1&branch=main"
        for attempt in range(4):
            try:
                async with aiohttp.ClientSession(headers=headers) as session:
                    async with session.get(url) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            runs = data.get("workflow_runs", [])
                            if runs:
                                return runs[0]
            except Exception as e:
                logger.warning(f"[Orchestrator] _get_latest_run attempt {attempt+1}: {e}")
            if attempt < 3:
                await asyncio.sleep(8)
        logger.error("[Orchestrator] _get_latest_run: no run found after 4 attempts")
        return None

    async def _get_run(self, cicd_agent, repo: str, run_id: int, token: str):
        import aiohttp
        headers = {
            "Authorization"       : f"Bearer {token}",
            "Accept"              : "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        url = f"https://api.github.com/repos/{repo}/actions/runs/{run_id}"
        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        return await resp.json()
        except Exception as e:
            logger.error(f"[Orchestrator] _get_run failed: {e}")
        return {}

    async def _get_run_logs(self, cicd_agent, repo: str, run_id: int, token: str):
        try:
            return await cicd_agent.collect_deployment_logs(str(run_id), repo)
        except Exception as e:
            logger.error(f"[Orchestrator] _get_run_logs failed: {e}")
            return []

    async def _push_to_github(self, project_path: str, repo_url: str, token: str) -> bool:
        import subprocess, os
        try:
            env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
            auth_url = repo_url.replace("https://", f"https://{token}@")

            def run(cmd):
                result = subprocess.run(cmd, capture_output=True, text=True, env=env)
                logger.debug("[git] %s -> rc=%d %s", cmd[-1], result.returncode, result.stderr.strip()[:120])
                return result

            # 1. init (safe to re-run on existing repo)
            run(["git", "-C", project_path, "init"])

            # 2. inject identity so commit never fails with "user not set"
            run(["git", "-C", project_path, "config", "user.email", "devops-agent@localhost"])
            run(["git", "-C", project_path, "config", "user.name",  "Autonomous DevOps Agent"])

            # 3. ensure we're on main branch
            run(["git", "-C", project_path, "checkout", "-B", "main"])

            # 4. stage everything
            r = run(["git", "-C", project_path, "add", "."])
            if r.returncode != 0:
                logger.error("[Orchestrator] git add failed: %s", r.stderr)
                return False

            # 5. commit — allowed to fail with rc=1 when "nothing to commit"
            r = run(["git", "-C", project_path, "commit", "-m", "chore: autonomous devops scaffold"])
            if r.returncode != 0 and "nothing to commit" not in r.stdout + r.stderr:
                logger.error("[Orchestrator] git commit failed: %s", r.stderr)
                return False

            # 6. set remote (remove stale origin first — safe to fail)
            run(["git", "-C", project_path, "remote", "remove", "origin"])
            r = run(["git", "-C", project_path, "remote", "add", "origin", auth_url])
            if r.returncode != 0:
                logger.error("[Orchestrator] git remote add failed: %s", r.stderr)
                return False

            # 7. push
            r = run(["git", "-C", project_path, "push", "--force", "-u", "origin", "main"])
            if r.returncode != 0:
                logger.error("[Orchestrator] git push failed: %s", r.stderr)
                return False

            logger.info("[Orchestrator] Git push succeeded → %s", repo_url)
            return True

        except Exception as e:
            logger.error(f"[Orchestrator] Git push failed: {e}")
            return False

    # ── handle_incident (called from CI/CD flow) ──────────────────────────────

    async def handle_incident(self, incident: Incident) -> None:
        """
        Called by CI/CD flow after log analysis produces an Incident.
        Publishes INCIDENT_CREATED so the normal event-driven flow handles it.
        """
        llm  = incident.metadata.get("llm_analysis", {})
        meta = incident.metadata

        # Human-readable label for the incident banner display
        _ISSUE_LABELS = {
            "syntax"      : "SYNTAX ERROR",
            "import"      : "IMPORT / DEPENDENCY ERROR",
            "runtime"     : "RUNTIME ERROR",
            "cicd_failure": "CI/CD PIPELINE FAILURE",
            "unknown"     : "UNCLASSIFIED ERROR",
        }
        raw_issue_type   = meta.get("issue_type", "unknown") or "unknown"
        issue_type_label = _ISSUE_LABELS.get(
            raw_issue_type,
            raw_issue_type.upper().replace("_", " "),
        )

        await self.event_bus.publish(Event(
            type=EventType.INCIDENT_CREATED,
            source="orchestrator",
            incident_id=incident.incident_id,
            data={
                "incident_id"      : incident.incident_id,
                "service"          : incident.service,
                "severity"         : incident.severity.value,
                "description"      : incident.description,
                "files_to_fix"     : llm.get("files_to_fix", []),
                "report"           : llm.get("report", ""),
                "impact"           : llm.get("impact", ""),
                "recommended"      : llm.get("recommended", ""),
                "confidence"       : llm.get("confidence", 0.0),
                # ── issue classification (from monitoring agent) ───────────
                "issue_type"       : raw_issue_type,
                "issue_type_label" : issue_type_label,
                "syntax_errors"    : llm.get("syntax_errors",
                                        meta.get("syntax_errors", [])),
                "has_syntax_error" : meta.get("has_syntax_error", False),
                "flawed_file"      : meta.get("flawed_file", ""),
                "all_flawed_files" : meta.get("all_flawed_files", []),
            }
        ))

    # ── Core incident handler ─────────────────────────────────────────────────

    async def _on_incident_created(self, event: Event) -> None:
        """
        Triggered by INCIDENT_CREATED.

        Routing logic:
          • issue_type == "syntax"  → SelfHealingAgent fixes the file directly
          • issue_type == anything else (runtime, import, unknown, …)
                                    → KnowledgeAgent investigates first,
                                      then SelfHealingAgent applies the fix,
                                      AND the user is shown manual instructions
                                      in case they prefer to act themselves.
        """
        logger.info("[Orchestrator] INCIDENT_CREATED → routing by issue_type")

        issue_type   = event.data.get("issue_type", "unknown")
        files_to_fix = event.data.get("files_to_fix", [])
        service      = event.data.get("service", "unknown")
        severity     = event.data.get("severity", "unknown")
        description  = event.data.get("description", "")

        self._dash("stage", "incident")
        self._track_incident(
            incident_id = event.incident_id,
            service     = service,
            severity    = severity,
            description = description,
            status      = "OPEN",
        )

        # ── Branch: SYNTAX ERROR → auto-fix directly ──────────────────────────
        if issue_type == "syntax":
            logger.info(
                "[Orchestrator] issue_type=syntax — routing directly to SelfHealingAgent"
            )
            self.print_dashboard(
                f"SYNTAX ERROR detected in {service} — auto-fix via SelfHealingAgent"
            )
            await self._heal_syntax_errors(event, files_to_fix)
            return

        # ── Branch: OTHER ERROR → show user instructions + Knowledge Agent ────
        logger.info(
            "[Orchestrator] issue_type=%s — showing user instructions + KnowledgeAgent",
            issue_type,
        )
        self._print_user_instructions(
            incident_id  = event.incident_id,
            service      = service,
            severity     = severity,
            issue_type   = issue_type,
            description  = description,
            files_to_fix = files_to_fix,
            event_data   = event.data,
        )

        await self._run_knowledge_then_heal(event, files_to_fix)

    # ── Syntax-error fast path ────────────────────────────────────────────────

    async def _heal_syntax_errors(self, event: Event, files_to_fix: list) -> None:
        """
        Skip the Knowledge Agent entirely for syntax errors.
        Build a minimal Solution and hand it straight to SelfHealingAgent.
        """
        if not files_to_fix:
            logger.warning(
                "[Orchestrator] Syntax error flagged but no files_to_fix — "
                "falling back to user instructions"
            )
            self._print_user_instructions(
                incident_id  = event.incident_id,
                service      = event.data.get("service", "unknown"),
                severity     = event.data.get("severity", "unknown"),
                issue_type   = "syntax",
                description  = event.data.get("description", ""),
                files_to_fix = [],
                event_data   = event.data,
            )
            return

        healing_agent = self.registry.get_agent("self_healing_agent")
        if not healing_agent:
            logger.error("[Orchestrator] SelfHealingAgent not registered!")
            return

        description = event.data.get("description", "")
        files_to_modify = [
            FileToFix.from_monitoring(entry)
            for entry in files_to_fix
            if entry.get("file") or entry.get("path")
        ]

        service     = event.data.get("service", "unknown")
        syntax_errs = event.data.get("syntax_errors", [])

        # Build a richer healing prompt using the raw error messages from the
        # monitoring agent so the LLM fixer has the exact error text
        if syntax_errs:
            error_lines = "\n".join(
                f"  • {e.get('error_type', 'SyntaxError')} in "
                f"{e.get('file', '?')} at line {e.get('line', '?')}: "
                f"{e.get('raw_message', e.get('message', ''))}"
                for e in syntax_errs
            )
            healing_prompt = (
                f"The CI/CD pipeline for '{service}' failed with syntax error(s).\n\n"
                f"Errors detected:\n{error_lines}\n\n"
                f"Fix the exact syntax/indentation error(s) listed above. "
                f"Do not refactor or rename anything else. "
                f"Only correct the broken line(s) so the file is valid Python."
            )
            suggested_cmds = [
                f"python -m py_compile {f.path}"
                for f in files_to_modify
                if f.path.endswith(".py")
            ]
        else:
            healing_prompt = (
                f"Fix the syntax error(s) in the file(s) listed. "
                f"Incident: {event.incident_id}. "
                f"Description: {description}"
            )
            suggested_cmds = []

        solution = SHSolution(
            incident_id        = event.incident_id,
            root_cause         = f"Syntax error in {service}: {description}",
            healing_prompt     = healing_prompt,
            confidence         = 0.95,
            suggested_commands = suggested_cmds,
            source             = "syntax_auto_fix",
            files_to_modify    = files_to_modify,
        )

        self._dashboard["agents"]["self_healing_agent"] = "RUNNING"
        self.state_manager.set_agent_status("self_healing_agent", AgentStatus.RUNNING)
        self.state_manager.update_incident_status(event.incident_id, IncidentStatus.REMEDIATING)
        self.print_dashboard(
            f"SelfHealingAgent fixing syntax in {len(files_to_modify)} file(s)"
        )

        try:
            result = await healing_agent.remediate(solution)

            logger.info(
                "[Orchestrator] Syntax auto-fix result: status=%s files=%d confidence=%.0f%%",
                result.status.value,
                len(result.file_modifications),
                result.confidence * 100,
            )

            if result.status == RemediationStatus.SUCCESS:
                await self.event_bus.publish(Event(
                    type=EventType.REMEDIATION_COMPLETE,
                    source="self_healing_agent",
                    incident_id=event.incident_id,
                    data={
                        "status"      : result.status.value,
                        "files_fixed" : [m.path for m in result.file_modifications if m.applied],
                        "confidence"  : result.confidence,
                        "steps"       : result.steps,
                        "verification": (
                            result.verification.status.value
                            if result.verification else "not_run"
                        ),
                        "commands_run": len(result.remediation_command_results),
                    },
                ))
            else:
                # Auto-fix failed — show user instructions so they can fix manually
                self._print_user_instructions(
                    incident_id  = event.incident_id,
                    service      = event.data.get("service", "unknown"),
                    severity     = event.data.get("severity", "unknown"),
                    issue_type   = "syntax",
                    description  = event.data.get("description", ""),
                    files_to_fix = files_to_fix,
                    event_data   = event.data,
                    extra_note   = "⚠  Automated fix failed — manual action required.",
                )
                await self.event_bus.publish(Event(
                    type=EventType.REMEDIATION_FAILED,
                    source="self_healing_agent",
                    incident_id=event.incident_id,
                    data={
                        "status" : result.status.value,
                        "errors" : result.validation_errors,
                        "files"  : [m.path for m in result.file_modifications],
                        "confidence": result.confidence,
                    },
                ))
        except Exception as e:
            logger.error(f"[Orchestrator] Syntax auto-fix exception: {e}", exc_info=True)
            await self.event_bus.publish(Event(
                type=EventType.REMEDIATION_FAILED,
                source="orchestrator",
                incident_id=event.incident_id,
                data={"status": RemediationStatus.FAILED.value, "errors": [str(e)]},
            ))
        finally:
            self._dashboard["agents"]["self_healing_agent"] = "IDLE"
            self.state_manager.set_agent_status("self_healing_agent", AgentStatus.IDLE)

    # ── Non-syntax path: Knowledge Agent + Self-Healing ───────────────────────

    async def _run_knowledge_then_heal(self, event: Event, files_to_fix: list) -> None:
        """
        Full investigation path for non-syntax errors:
        KnowledgeAgent → solution → SelfHealingAgent.

        KEY CHANGE: if the Knowledge Agent fails for any reason (e.g. Qdrant
        collection doesn't exist, Ollama unreachable, etc.) we do NOT stop.
        We fall back to a minimal Solution built directly from the incident
        payload — which already contains the file + line from the monitoring
        agent — and pass it straight to SelfHealingAgent.

        Self-healing is independent of the Knowledge Agent. It only needs
        to know WHAT file to fix and WHERE the error is, both of which the
        monitoring agent already provides in the event payload.
        """
        approved = await self.approval.request_approval(
            title="Incident detected — run Knowledge Agent to investigate?",
            details=[
                f"Incident  : {event.incident_id}",
                f"Service   : {event.data.get('service', 'unknown')}",
                f"Severity  : {event.data.get('severity', 'unknown')}",
                f"Desc      : {event.data.get('description', '')}",
                f"Files     : {len(files_to_fix)} file(s) identified by log parser",
                *[f"  → {f.get('file', '?')}:{f.get('line', '?')}" for f in files_to_fix[:3]],
            ],
        )
        if not approved:
            logger.info("[Orchestrator] Knowledge Agent cancelled.")
            return

        description = event.data.get("description", "")
        report      = event.data.get("report", "")
        impact      = event.data.get("impact", "")
        recommended = event.data.get("recommended", "")
        issue_type  = event.data.get("issue_type", "unknown")
        service     = event.data.get("service", "unknown")
        syntax_errs = event.data.get("syntax_errors", [])

        error_parts = [description]
        if report:
            error_parts.append(f"Incident report: {report}")
        if impact:
            error_parts.append(f"Impact: {impact}")
        if recommended:
            error_parts.append(f"Recommended: {recommended}")
        error_message = "\n".join(error_parts)

        # ── Step A: try the Knowledge Agent ──────────────────────────────────
        solution = None
        knowledge_agent = self.registry.get_agent("knowledge_agent")

        if knowledge_agent:
            self._dashboard["agents"]["knowledge_agent"] = "RUNNING"
            self.state_manager.set_agent_status("knowledge_agent", AgentStatus.RUNNING)
            self.print_dashboard(f"Knowledge Agent investigating incident {event.incident_id}")

            try:
                extra = {
                    "files_to_fix": files_to_fix,
                    "impact":       impact,
                    "recommended":  recommended,
                    "report":       report,
                }
                agent_response = knowledge_agent.run(error_message, extra=extra)

                files_to_modify = [
                    FileToFix.from_monitoring(entry)
                    for entry in files_to_fix
                    if entry.get("file") or entry.get("path")
                ]

                solution = SHSolution(
                    incident_id        = event.incident_id,
                    root_cause         = getattr(agent_response.rag_result, "root_cause", "")
                                         or error_message,
                    healing_prompt     = agent_response.healing_prompt,
                    confidence         = agent_response.confidence,
                    suggested_commands = agent_response.suggested_commands,
                    references         = list(agent_response.web_sources),
                    source             = agent_response.source.value,
                    files_to_modify    = files_to_modify,
                )

                logger.info(
                    "[Orchestrator] Knowledge Agent complete — "
                    "source=%s confidence=%.0f%% files_to_modify=%d",
                    solution.source,
                    solution.confidence * 100,
                    len(solution.files_to_modify),
                )
                self.print_dashboard(
                    f"Knowledge Agent complete — source={solution.source} "
                    f"confidence={solution.confidence:.0%} files={len(solution.files_to_modify)}"
                )
                self.state_manager.add_solution(solution)

                # Publish INVESTIGATION_COMPLETE for any other listeners
                await self.event_bus.publish(Event(
                    type=EventType.INVESTIGATION_COMPLETE,
                    source="knowledge_agent",
                    incident_id=event.incident_id,
                    data={"solution": solution},
                ))

            except Exception as e:
                logger.error(
                    "[Orchestrator] Knowledge Agent failed: %s — "
                    "falling back to direct self-healing from incident payload", e,
                    exc_info=True,
                )
                self.print_dashboard(
                    f"Knowledge Agent unavailable ({type(e).__name__}) "
                    f"— running self-healing directly from incident payload"
                )
                # solution stays None → fallback kicks in below

            finally:
                self._dashboard["agents"]["knowledge_agent"] = "IDLE"
                self.state_manager.set_agent_status("knowledge_agent", AgentStatus.IDLE)

        else:
            logger.warning("[Orchestrator] Knowledge Agent not registered — going straight to self-healing")
            self.print_dashboard("Knowledge Agent not registered — using fallback self-healing")

        # ── Step B: fallback solution from incident payload ───────────────────
        # Runs when Knowledge Agent failed OR is not registered.
        if solution is None:
            solution = self._build_fallback_solution(
                incident_id   = event.incident_id,
                service       = service,
                issue_type    = issue_type,
                description   = description,
                files_to_fix  = files_to_fix,
                syntax_errors = syntax_errs,
            )

        if solution is None:
            logger.error(
                "[Orchestrator] Could not build any solution for %s — "
                "no files identified. Manual intervention required.",
                event.incident_id,
            )
            return

        # ── Step C: self-healing always runs ─────────────────────────────────
        # INVESTIGATION_COMPLETE was already published when KA succeeded, so
        # _on_investigation_complete will call remediate() in that case.
        # Only invoke self-healing directly here in the fallback path
        # (KA failed or not registered) to avoid double-execution.
        #
        # BUG FIX: the old check `if knowledge_agent and ...` was always True
        # because `knowledge_agent` holds the agent object regardless of whether
        # the KA call succeeded or raised an exception. We now check `solution`
        # origin instead: if the KA succeeded it published INVESTIGATION_COMPLETE
        # and set solution.source != 'fallback', so we can safely return and let
        # _on_investigation_complete handle self-healing. If solution came from
        # _build_fallback_solution, we must invoke self-healing directly here.
        ka_succeeded = (
            knowledge_agent is not None
            and solution is not None
            and getattr(solution, "source", "fallback") != "fallback"
        )
        if ka_succeeded:
            # KA succeeded → _on_investigation_complete handles self-healing
            return

        # Fallback path — directly invoke self-healing
        healing_agent = self.registry.get_agent("self_healing_agent")
        if not healing_agent:
            logger.error("[Orchestrator] SelfHealingAgent not registered — cannot heal!")
            return

        self._dashboard["agents"]["self_healing_agent"] = "RUNNING"
        self.state_manager.set_agent_status("self_healing_agent", AgentStatus.RUNNING)
        self.state_manager.update_incident_status(event.incident_id, IncidentStatus.REMEDIATING)
        self.print_dashboard(
            f"SelfHealingAgent (fallback) fixing {len(solution.files_to_modify)} file(s)"
        )

        try:
            result = await healing_agent.remediate(solution)
            logger.info(
                "[Orchestrator] Fallback self-healing result: status=%s files=%d confidence=%.0f%%",
                result.status.value,
                len(result.file_modifications),
                result.confidence * 100,
            )

            if result.status == RemediationStatus.SUCCESS:
                await self.event_bus.publish(Event(
                    type=EventType.REMEDIATION_COMPLETE,
                    source="self_healing_agent",
                    incident_id=event.incident_id,
                    data={
                        "status"      : result.status.value,
                        "files_fixed" : [m.path for m in result.file_modifications if m.applied],
                        "confidence"  : result.confidence,
                        "steps"       : result.steps,
                        "verification": (
                            result.verification.status.value
                            if result.verification else "not_run"
                        ),
                        "commands_run": len(result.remediation_command_results),
                    },
                ))
            else:
                await self.event_bus.publish(Event(
                    type=EventType.REMEDIATION_FAILED,
                    source="self_healing_agent",
                    incident_id=event.incident_id,
                    data={
                        "status"    : result.status.value,
                        "errors"    : result.validation_errors,
                        "files"     : [m.path for m in result.file_modifications],
                        "confidence": result.confidence,
                    },
                ))

        except Exception as e:
            logger.error("[Orchestrator] Fallback self-healing raised: %s", e, exc_info=True)
            await self.event_bus.publish(Event(
                type=EventType.REMEDIATION_FAILED,
                source="orchestrator",
                incident_id=event.incident_id,
                data={"status": RemediationStatus.FAILED.value, "errors": [str(e)]},
            ))
        finally:
            self._dashboard["agents"]["self_healing_agent"] = "IDLE"
            self.state_manager.set_agent_status("self_healing_agent", AgentStatus.IDLE)

    def _build_fallback_solution(
        self,
        incident_id   : str,
        service       : str,
        issue_type    : str,
        description   : str,
        files_to_fix  : list,
        syntax_errors : list,
    ) -> "SHSolution | None":
        """
        Build a minimal Solution directly from the INCIDENT_CREATED event payload
        when the Knowledge Agent is unavailable (Qdrant not initialized, etc.).

        The monitoring agent already provides the exact file + line in the
        event payload — we don't need RAG to know what to fix.
        """
        files_to_modify = []

        # Prefer syntax_errors list (has error_type + raw_message)
        for err in syntax_errors:
            f = err.get("file", "").strip()
            if not f:
                continue
            files_to_modify.append(FileToFix(
                path            = f,
                line            = int(err.get("line", 0) or 0),
                function        = "<module>",
                exception       = (
                    f"{err.get('error_type', 'SyntaxError')}: "
                    f"{err.get('raw_message', err.get('message', ''))}"
                ),
                fix_description = (
                    f"Fix {err.get('error_type', 'SyntaxError')} at line "
                    f"{err.get('line', '?')}: "
                    f"{err.get('raw_message', err.get('message', ''))}"
                ),
            ))

        # Fall back to generic files_to_fix from the LLM analyzer
        if not files_to_modify:
            for entry in files_to_fix:
                f = (entry.get("file") or entry.get("path", "")).strip()
                if not f:
                    continue
                files_to_modify.append(FileToFix.from_monitoring(entry))

        if not files_to_modify:
            logger.warning(
                "[Orchestrator] _build_fallback_solution: no files identified "
                "for incident %s — cannot self-heal", incident_id,
            )
            return None

        healing_prompt = (
            f"The CI/CD pipeline for '{service}' failed.\n\n"
            f"Issue type : {issue_type}\n"
            f"Description: {description}\n\n"
        )
        if syntax_errors:
            healing_prompt += "Errors:\n" + "\n".join(
                f"  • {e.get('error_type', 'SyntaxError')} in "
                f"{e.get('file', '?')} at line {e.get('line', '?')}: "
                f"{e.get('raw_message', e.get('message', ''))}"
                for e in syntax_errors
            )
        healing_prompt += (
            "\n\nFix the exact error(s) listed above. "
            "Preserve all logic unrelated to the error. "
            "Do not rename or refactor anything."
        )

        return SHSolution(
            incident_id        = incident_id,
            root_cause         = description or f"CI/CD failure in {service}",
            healing_prompt     = healing_prompt,
            confidence         = 0.80,
            suggested_commands = [
                f"python -m py_compile {f.path}"
                for f in files_to_modify
                if f.path.endswith(".py")
            ],
            files_to_modify    = files_to_modify,
            source             = "fallback_incident_payload",
        )

    # ── User instructions printer ─────────────────────────────────────────────

    def _print_user_instructions(
        self,
        incident_id  : str,
        service      : str,
        severity     : str,
        issue_type   : str,
        description  : str,
        files_to_fix : list,
        event_data   : dict,
        extra_note   : str = "",
    ) -> None:
        """
        Print a clear, human-readable instruction panel for non-syntax errors
        (or when the auto-fix fails).  Tells the operator exactly what went wrong,
        which files to look at, and what steps to take.
        """
        B, R, RD, YL, CY, GR, DM = (
            _B, _R, _RD, _YL, _CY, _GR, _DM,
        )
        W = 70

        sev_col = {
            "critical": RD, "high": RD, "medium": YL, "low": GR,
        }.get(severity.lower(), YL)

        # Prefer pre-formatted label from the event payload if available
        type_label = event_data.get("issue_type_label") or {
            "syntax"      : "SYNTAX ERROR",
            "runtime"     : "RUNTIME ERROR",
            "import"      : "IMPORT / DEPENDENCY ERROR",
            "cicd_failure": "CI/CD PIPELINE FAILURE",
            "unknown"     : "UNCLASSIFIED ERROR",
        }.get(issue_type, issue_type.upper().replace("_", " ") + " ERROR")

        print(f"\n{B}{RD}  {'▲ INCIDENT REQUIRES ATTENTION ':=<{W}}{R}")
        print(f"  {B}Incident ID{R} : {incident_id}")
        print(f"  {B}Service    {R} : {service}")
        print(f"  {B}Severity   {R} : {sev_col}{severity.upper()}{R}")
        print(f"  {B}Error Type {R} : {YL}{type_label}{R}")
        print(f"  {B}Description{R} : {description[:120]}")

        if extra_note:
            print(f"\n  {RD}{B}{extra_note}{R}")

        report = event_data.get("report", "")
        impact = event_data.get("impact", "")
        recommended = event_data.get("recommended", "")
        if report:
            print(f"\n  {B}Analysis   {R}: {DM}{report[:200]}{R}")
        if impact:
            print(f"  {B}Impact     {R}: {DM}{impact[:200]}{R}")
        if recommended:
            print(f"  {B}Recommended{R}: {DM}{recommended[:200]}{R}")

        if files_to_fix:
            print(f"\n  {B}{CY}FILES TO INVESTIGATE ({len(files_to_fix)}){R}")
            print(f"  {'─' * W}")
            for i, f in enumerate(files_to_fix, 1):
                path      = f.get("file") or f.get("path", "?")
                line      = f.get("line", "?")
                func      = f.get("function", "?")
                exc       = f.get("exception", "")
                hint      = f.get("fix_description", "")
                itype     = f.get("issue_type", issue_type)
                print(f"  {B}[{i}]{R} {CY}{path}{R}  line {YL}{line}{R}  in {func}()")
                if exc:
                    print(f"       {DM}Exception : {exc}{R}")
                if hint:
                    print(f"       {GR}Fix hint  : {hint}{R}")
                if itype and itype != "unknown":
                    print(f"       {DM}Type      : {itype}{R}")

        print(f"\n  {B}SUGGESTED MANUAL STEPS{R}")
        print(f"  {'─' * W}")

        if issue_type == "import":
            print(f"  {GR}1.{R} Check your requirements.txt / pyproject.toml for missing packages")
            print(f"  {GR}2.{R} Run:  {CY}pip install -r requirements.txt{R}")
            print(f"  {GR}3.{R} Verify the import path — the module may have been renamed or moved")
            print(f"  {GR}4.{R} Re-run your tests after installing: {CY}pytest{R}")
        elif issue_type == "runtime":
            print(f"  {GR}1.{R} Open each file listed above and inspect the line number shown")
            print(f"  {GR}2.{R} Check environment variables / secrets referenced in that function")
            print(f"  {GR}3.{R} Review recent commits for changes near the flagged line:")
            print(f"        {CY}git log --oneline -10{R}")
            print(f"  {GR}4.{R} Run the affected function with a test harness to reproduce locally")
            print(f"  {GR}5.{R} Fix and redeploy — the system will monitor the next run")
        else:  # unknown / other
            print(f"  {GR}1.{R} Review the error description and report printed above")
            print(f"  {GR}2.{R} Open the files listed and look at the line numbers flagged")
            print(f"  {GR}3.{R} Search your codebase for the exception type mentioned:")
            print(f"        {CY}grep -r \"{description[:40]}\" .{R}")
            print(f"  {GR}4.{R} Check recent changes: {CY}git diff HEAD~1{R}")
            print(f"  {GR}5.{R} If the system proceeds with automated healing, review the diff")
            print(f"        before merging any auto-generated changes")

        print(f"\n  {DM}The Knowledge Agent will now investigate and the Self-Healing Agent")
        print(f"  will attempt an automated fix.  You can also act on the steps above")
        print(f"  in parallel — the first successful resolution wins.{R}")
        print(f"  {B}{RD}  {'═' * W}{R}\n")

    # ── Investigation complete → Self-Healing ─────────────────────────────────

    async def _on_investigation_complete(self, event: Event) -> None:
        """
        Triggered by INVESTIGATION_COMPLETE.
        Routes solution to SelfHealingAgent.
        """
        logger.info("[Orchestrator] INVESTIGATION_COMPLETE → Self-Healing Agent")

        solution: SHSolution = event.data.get("solution")
        if not solution:
            logger.error("[Orchestrator] INVESTIGATION_COMPLETE event missing 'solution'")
            return

        approved = await self.approval.request_approval(
            title="Investigation complete — apply self-healing fix?",
            details=[
                f"Root cause : {solution.root_cause[:100]}",
                f"Confidence : {solution.confidence:.0%}",
                f"Source     : {solution.source}",
                f"Files      : {len(solution.files_to_modify)} file(s) to modify",
                *[f"  → {f.path}:{f.line}  ({f.exception})" for f in solution.files_to_modify[:3]],
            ],
        )
        if not approved:
            logger.info("[Orchestrator] Self-Healing cancelled.")
            return

        healing_agent = self.registry.get_agent("self_healing_agent")
        if not healing_agent:
            logger.error("[Orchestrator] Self-Healing Agent not registered!")
            return

        self._dash("stage", "healing")
        self._dashboard["agents"]["self_healing_agent"] = "RUNNING"
        self.state_manager.set_agent_status("self_healing_agent", AgentStatus.RUNNING)
        self.state_manager.update_incident_status(event.incident_id, IncidentStatus.REMEDIATING)
        self.print_dashboard(f"Self-Healing Agent applying fix — {len(solution.files_to_modify)} file(s)")

        try:
            result = await healing_agent.remediate(solution)

            logger.info(
                "[Orchestrator] Self-Healing result: status=%s files=%d confidence=%.0f%% verification=%s",
                result.status.value,
                len(result.file_modifications),
                result.confidence * 100,
                result.verification.status.value if result.verification else "not_run",
            )

            if result.status == RemediationStatus.SUCCESS:
                await self.event_bus.publish(Event(
                    type=EventType.REMEDIATION_COMPLETE,
                    source="self_healing_agent",
                    incident_id=event.incident_id,
                    data={
                        "status"      : result.status.value,
                        "files_fixed" : [m.path for m in result.file_modifications if m.applied],
                        "confidence"  : result.confidence,
                        "steps"       : result.steps,
                        "verification": (
                            result.verification.status.value
                            if result.verification else "not_run"
                        ),
                        "commands_run": len(result.remediation_command_results),
                    },
                ))
            else:
                await self.event_bus.publish(Event(
                    type=EventType.REMEDIATION_FAILED,
                    source="self_healing_agent",
                    incident_id=event.incident_id,
                    data={
                        "status"    : result.status.value,
                        "errors"    : result.validation_errors,
                        "files"     : [m.path for m in result.file_modifications],
                        "confidence": result.confidence,
                    },
                ))

        except Exception as e:
            logger.error(f"[Orchestrator] Self-Healing Agent failed: {e}", exc_info=True)
            self.print_dashboard(f"Self-Healing Agent exception: {e}")
            await self.event_bus.publish(Event(
                type=EventType.REMEDIATION_FAILED,
                source="orchestrator",
                incident_id=event.incident_id,
                data={
                    "status": RemediationStatus.FAILED.value,
                    "errors": [f"Unhandled exception: {e}"],
                },
            ))
        finally:
            self._dashboard["agents"]["self_healing_agent"] = "IDLE"
            self.state_manager.set_agent_status("self_healing_agent", AgentStatus.IDLE)

    # ── Remediation outcomes ──────────────────────────────────────────────────

    async def _on_remediation_complete(self, event: Event) -> None:
        files_fixed  = event.data.get("files_fixed", [])
        verification = event.data.get("verification", "not_run")
        logger.info(
            "[Orchestrator] REMEDIATION_COMPLETE — incident=%s files=%s verification=%s",
            event.incident_id, files_fixed, verification,
        )
        self.state_manager.update_incident_status(event.incident_id, IncidentStatus.RESOLVED)
        self._track_incident(
            incident_id = event.incident_id,
            service     = "",
            severity    = "",
            description = "",
            status      = "RESOLVED",
        )
        self._dash("stage", "done")
        self.print_dashboard(
            f"RESOLVED — {len(files_fixed)} file(s) fixed, verification={verification}"
        )
        inc_rec  = next((r for r in self._dashboard["incidents"] if r["id"] == event.incident_id), {})
        severity = inc_rec.get("severity", "")
        await self._send_alert(
            incident_id=event.incident_id,
            title="Incident Resolved",
            message=(
                f"Incident {event.incident_id} resolved automatically. "
                f"Fixed {len(files_fixed)} file(s). "
                f"Verification: {verification}."
            ),
            severity=severity,
        )
        self.context_manager.drop_context(event.incident_id)

    async def _on_remediation_failed(self, event: Event) -> None:
        errors = event.data.get("errors", [])
        logger.warning(
            "[Orchestrator] REMEDIATION_FAILED — incident=%s errors=%s",
            event.incident_id, errors,
        )
        self.state_manager.update_incident_status(event.incident_id, IncidentStatus.FAILED)
        self._track_incident(
            incident_id = event.incident_id,
            service     = "",
            severity    = "",
            description = "",
            status      = "FAILED",
        )
        self._dash("stage", "done")
        self.print_dashboard(
            f"REMEDIATION FAILED — {errors[0] if errors else 'unknown error'}"
        )
        inc_rec  = next((r for r in self._dashboard["incidents"] if r["id"] == event.incident_id), {})
        severity = inc_rec.get("severity", "high")
        await self._send_alert(
            incident_id=event.incident_id,
            title="Remediation Failed",
            message=(
                f"Incident {event.incident_id} could not be resolved automatically. "
                f"Errors: {errors}. Manual intervention required."
            ),
            severity=severity,
        )

    async def _send_alert(
        self, incident_id: str, title: str, message: str, severity: str = ""
    ) -> None:
        """
        Send a one-way alert via email.
        HIGH / CRITICAL severity activates the emergency lane (full team).
        Also calls the legacy alerting_agent if registered.
        """
        severity_upper = severity.upper()
        urgent = (
            severity_upper in ("HIGH", "CRITICAL")
            or any(kw in title.lower() for kw in ("failed", "critical", "manual action"))
        )

        if self._email:
            try:
                await self._email.send_alert(title=title, message=message, urgent=urgent)
            except Exception as exc:
                logger.error("[Orchestrator] Email alert failed: %s", exc)

        alerting_agent = self.registry.get_agent("alerting_agent")
        if alerting_agent:
            try:
                await alerting_agent.send(incident_id=incident_id, title=title, message=message)
            except Exception as e:
                logger.error(f"[Orchestrator] Alerting Agent failed: {e}")

    def summary(self) -> dict:
        return {
            "orchestrator" : "running" if self._running else "stopped",
            "agents"       : self.registry.summary(),
            "state"        : self.state_manager.summary(),
            "event_history": len(self.event_bus.get_history()),
        }

    def __repr__(self):
        return f"Orchestrator(running={self._running}, agents={self.registry.get_all_names()})"