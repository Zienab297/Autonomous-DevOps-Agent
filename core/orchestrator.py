"""
core/orchestrator.py

Incident flow (Monitor → Knowledge → Self-Healing):
----------------------------------------------------
1. MonitoringAgent detects anomaly, publishes INCIDENT_CREATED
   event.data includes:
       - incident_id, service, severity, description
       - files_to_fix: [{file, line, function, exception, fix_description}, ...]

2. _on_incident_created:
       - saves files_to_fix from event.data locally
       - [APPROVAL] run Knowledge Agent?
       - calls knowledge_agent.run(error_message) → AgentResponse
       - checks source:
           RAGSource.KNOWLEDGE_BASE → [APPROVAL] apply RAG solution?
           RAGSource.LLM_GENERATED  → [APPROVAL] use generated solution?
       - if approved: builds Solution, publishes INVESTIGATION_COMPLETE

3. _on_investigation_complete:
       - [APPROVAL] apply self-healing fix?
       - calls self_healing_agent.remediate(solution) → SelfHealingResult
       - publishes REMEDIATION_COMPLETE or REMEDIATION_FAILED

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

from agents.self_healing_agent.models import FileToFix, Solution as SHSolution
from agents.monitoring_agent.agent import MonitoringAgent
from agents.monitoring_agent.config import MonitoringConfig

# Knowledge base source enum — imported lazily inside methods to avoid
# circular imports, but aliased here for type hints
try:
    from agents.knowledge_agent.shared.models import RAGSource
except Exception:
    RAGSource = None

logger = logging.getLogger(__name__)


class Orchestrator:

    def __init__(self):
        self.event_bus       = EventBus()
        self.state_manager   = StateManager()
        self.context_manager = ContextManager()
        self.registry        = AgentRegistry()
        self.approval        = ApprovalManager()
        self.approval.registry = self.registry   # pause monitoring dashboard during input
        self._running        = False

        self._subscribe_to_events()
        self._register_monitoring_agent()
        self._register_knowledge_agent()

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

    def _register_monitoring_agent(self) -> None:
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
        agent = self.registry.get_agent("monitoring_agent")
        if agent is None:
            logger.error("[Orchestrator] MonitoringAgent not found in registry")
            return
        if getattr(agent, "_poll_task", None) and not agent._poll_task.done():
            logger.debug("[Orchestrator] MonitoringAgent already running — skipping start")
            return
        await agent.start()
        logger.info("[Orchestrator] MonitoringAgent started")

    def _register_knowledge_agent(self) -> None:
        if self.registry.get_agent("knowledge_agent"):
            return

        try:
            # ── IMPORTANT: do NOT add knowledge_agent root to sys.path ────
            # Adding agents/knowledge_agent/ causes "shared" to resolve to
            # scaffold_agent/shared instead of knowledge_agent/shared because
            # scaffold's setup_path.py inserts scaffold_agent/ earlier.
            # We use full absolute package paths (agents.knowledge_agent.*)
            # throughout — no bare "shared.*" imports anywhere in this method.

            _project_root = pathlib.Path(__file__).resolve().parents[1]
            if str(_project_root) not in sys.path:
                sys.path.insert(0, str(_project_root))

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
            # Force remove scaffold's 'shared' from Python module cache
            for key in list(sys.modules.keys()):
                if key == "shared" or key.startswith("shared."):
                    del sys.modules[key]

            from agents.knowledge_agent.knowledge_core.knowledge_agent_adapter import KnowledgeAgentAdapter
            agent = KnowledgeAgentAdapter()

            # Import adapter using full absolute path — never bare "shared.*"
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
            lines.append(f"  {sym} {name:<24} {col}{raw_status}{R}")

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

    async def run_scaffold(self, project_path: str, dry_run: bool = False) -> None:
        logger.info(f"[Orchestrator] run_scaffold: {project_path}")
        await self.event_bus.publish(Event(
            type=EventType.SCAFFOLD_STARTED,
            source="cli",
            data={"project_path": project_path, "dry_run": dry_run},
        ))

    async def _on_scaffold_started(self, event: Event) -> None:
        self._dash("stage",   "scaffold")
        self._dash("project", event.data.get("project_path", ""))
        self.print_dashboard("ScaffoldAgent started")
        logger.info("[Orchestrator] SCAFFOLD_STARTED → ScaffoldAgent")

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
        project_path = event.data.get("project_path")
        dry_run      = event.data.get("dry_run", False)

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
            self.print_dashboard(
                f"Scaffold complete — {result.framework.value} ({result.language.value}), "
                f"{len(result.generated_files)} files generated"
            )
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

        # ── APPROVAL 1: proceed to CI/CD? ─────────────────────────────────
        approved = await self.approval.request_approval(
            title=f"✅ Scaffold complete — {framework} ({language}). Proceed to CI/CD?",
            details=files,
            context={"project_path": project_path},
        )
        if not approved:
            logger.info("[Orchestrator] CI/CD cancelled by developer.")
            print("\n  Pipeline stopped. No CI/CD triggered.\n")
            self._dash("stage", "done")
            return

        repo_url = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: input("\n  GitHub repo URL (https://github.com/user/repo): ").strip()
        )
        if not repo_url:
            print("  No repo URL — stopping.\n")
            self._dash("stage", "done")
            return

        import os
        token = os.getenv("GITHUB_TOKEN", "")
        if not token:
            print("  No GITHUB_TOKEN in .env — stopping.\n")
            self._dash("stage", "done")
            return
        print("  [OK]  GITHUB_TOKEN loaded from .env")

        pushed = await self._push_to_github(
            project_path=project_path,
            repo_url=repo_url,
            token=token,
        )
        if not pushed:
            self.print_dashboard("Git push failed — stopping")
            self._dash("stage", "done")
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

            print("  Waiting for GitHub Actions to start...")
            await asyncio.sleep(10)

            run = await self._get_latest_run(cicd_agent, repo, token)

            if not run:
                self._dash("cicd_status", "no run found")
                self.print_dashboard("Could not find pipeline run — workflow may not have started yet")
                logs = []
            else:
                print(f"  Run ID : {run.id}")
                print(f"  URL    : {run.url}")

                deadline = 120
                elapsed  = 0
                while run.status not in ("success", "failed", "cancelled") and elapsed < deadline:
                    await asyncio.sleep(8)
                    elapsed += 8
                    run = await cicd_agent.get_pipeline_status(run.id, repo)
                    self._dash("cicd_status", f"{run.status} ({elapsed}s)")
                    print(f"  [{elapsed}s] status: {run.status}")

                self._dash("cicd_status", run.status)
                logs = await cicd_agent.collect_deployment_logs(run.id, repo)
                self.print_dashboard(
                    f"CI/CD finished — {run.status.upper()}, {len(logs)} log lines collected"
                )
                print(f"\n  -- CI/CD Logs ({len(logs)} lines) --")
                for line in logs:
                    print(f"    {line}")
                print("  --------------------------------\n")

            await self.event_bus.publish(Event(
                type=EventType.DEPLOYMENT_COMPLETE,
                source="cicd_agent",
                data={
                    "project_path": project_path,
                    "repo_url"    : repo_url,
                    "logs"        : logs,
                    "status"      : run.status if run else "unknown",
                },
            ))

            if logs:
                # ── APPROVAL 2: run monitoring on CI/CD logs? ──────────────
                approved = await self.approval.request_approval(
                    title=f"CI/CD {'succeeded' if run and run.status == 'success' else 'finished'} — run Monitoring Agent to analyze logs?",
                    details=logs[:10],
                    context={"project_path": project_path},
                )
                if not approved:
                    logger.info("[Orchestrator] Monitoring Agent cancelled.")
                    print("\n  Monitoring skipped.\n")
                    self._dash("stage", "done")
                    return

                monitoring_agent = self.registry.get_agent("monitoring_agent")
                if not monitoring_agent:
                    logger.error("[Orchestrator] MonitoringAgent not registered — cannot analyze logs")
                    self._dash("stage", "done")
                    return

                await self.start_monitoring_agent()

                self._dash("stage", "monitoring")
                self._dashboard["agents"]["monitoring_agent"] = "RUNNING"
                self.state_manager.set_agent_status("monitoring_agent", AgentStatus.RUNNING)
                self.print_dashboard("Monitoring Agent analyzing CI/CD logs")

                try:
                    incident = await monitoring_agent.analyze_logs(logs)

                    if incident:
                        self._dash("stage", "incident")
                        self._track_incident(
                            incident_id = incident.incident_id,
                            service     = incident.service,
                            severity    = incident.severity.value,
                            description = incident.description,
                            status      = "OPEN",
                        )
                        files_to_fix = incident.metadata.get("llm_analysis", {}).get("files_to_fix", [])
                        fix_note = f" — {len(files_to_fix)} file(s) to fix" if files_to_fix else ""
                        self.print_dashboard(
                            f"Incident detected [{incident.severity.value.upper()}] "
                            f"on {incident.service}{fix_note}"
                        )
                        await self.handle_incident(incident)
                    else:
                        self._dash("stage", "done")
                        self.print_dashboard("Monitoring complete — no incidents detected, system healthy")
                finally:
                    self._dashboard["agents"]["monitoring_agent"] = "IDLE"
                    self.state_manager.set_agent_status("monitoring_agent", AgentStatus.IDLE)

        except Exception as e:
            logger.error(f"[Orchestrator] CI/CD Agent failed: {e}")
            self._dash("cicd_status", f"error: {e}")
            self.print_dashboard(f"CI/CD Agent error: {e}")
            await self.event_bus.publish(Event(
                type=EventType.DEPLOYMENT_COMPLETE,
                source="cicd_agent",
                data={"project_path": project_path, "logs": [], "repo_url": repo_url},
            ))
        finally:
            self._dashboard["agents"]["cicd_agent"] = "IDLE"
            self.state_manager.set_agent_status("cicd_agent", AgentStatus.IDLE)

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
                                run_id = str(runs[0]["id"])
                                return await cicd_agent.get_pipeline_status(run_id, repo)
            except Exception as e:
                logger.warning(f"[Orchestrator] get_latest_run attempt {attempt+1}: {e}")

            if attempt < 3:
                print(f"  Waiting for run to appear... (attempt {attempt+2}/4)")
                await asyncio.sleep(8)

        return None

    async def _on_scaffold_failed(self, event: Event) -> None:
        err = event.data.get("error", "unknown")
        logger.error(f"[Orchestrator] Scaffold failed: {err}")
        self._dash("stage", "done")
        self.print_dashboard(f"Scaffold FAILED: {err}")

    async def _push_to_github(self, project_path: str, repo_url: str, token: str) -> bool:
        import subprocess

        if repo_url.startswith("https://"):
            auth_url = repo_url.replace("https://", f"https://{token}@")
        else:
            auth_url = repo_url

        def run(cmd: list, cwd: str) -> tuple[int, str]:
            result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
            return result.returncode, result.stdout + result.stderr

        print(f"\n  Pushing to: {repo_url}")

        _OPTIONAL = {"remove old remote (ok if fails)", "git commit"}

        steps = [
            (["git", "init"],                                    "git init"),
            (["git", "add", "."],                                "git add"),
            (["git", "commit", "-m", "chore: add DevOps scaffold files"], "git commit"),
            (["git", "branch", "-M", "main"],                    "git branch"),
            (["git", "remote", "remove", "origin"],              "remove old remote (ok if fails)"),
            (["git", "remote", "add", "origin", auth_url],       "git remote add"),
            (["git", "push", "-u", "origin", "main", "--force"], "git push"),
        ]

        for cmd, label in steps:
            code, out = run(cmd, project_path)
            if code != 0 and label not in _OPTIONAL:
                print(f"  Failed at [{label}]: {out.strip()[:200]}")
                return False
            else:
                print(f"  [{label}] OK")

        print(f"  Pushed successfully to {repo_url}\n")
        return True

    # ── Incident Workflow ─────────────────────────────────────────────────────

    async def handle_incident(self, incident: Incident) -> None:
        logger.info(f"[Orchestrator] Handling: {incident}")

        self.state_manager.add_incident(incident)
        self.state_manager.update_incident_status(incident.incident_id, IncidentStatus.INVESTIGATING)

        if not self.context_manager.get_context(incident.incident_id):
            self.context_manager.create_context(incident)
            self.context_manager.add_metrics(incident.incident_id, incident.metrics)
            self.context_manager.add_logs(incident.incident_id, incident.logs)

        llm = incident.metadata.get("llm_analysis", {})
        await self.event_bus.publish(Event(
            type=EventType.INCIDENT_CREATED,
            source="orchestrator",
            incident_id=incident.incident_id,
            data={
                "incident_id" : incident.incident_id,
                "service"     : incident.service,
                "severity"    : incident.severity.value,
                "description" : incident.description,
                "files_to_fix": llm.get("files_to_fix", []),
                "report"      : llm.get("report", ""),
                "impact"      : llm.get("impact", ""),
                "recommended" : llm.get("recommended", ""),
                "confidence"  : llm.get("confidence", 0.0),
            }
        ))

    async def _on_incident_created(self, event: Event) -> None:
        """
        INCIDENT_CREATED handler.

        Approval flow:
          1. [APPROVAL] Run Knowledge Agent?
          2. Knowledge Agent runs → checks source
             - KNOWLEDGE_BASE → [APPROVAL] Apply RAG solution?
             - LLM_GENERATED  → [APPROVAL] Use generated/web solution?
          3. If approved → publish INVESTIGATION_COMPLETE
        """
        logger.info("[Orchestrator] INCIDENT_CREATED → Knowledge Agent")

        files_to_fix: list = event.data.get("files_to_fix", [])

        # ── APPROVAL 3: run Knowledge Agent? ──────────────────────────────
        file_preview = [
            f"  → {f.get('file', '?')}:{f.get('line', '?')}"
            for f in files_to_fix[:3]
        ]
        approved = await self.approval.request_approval(
            title="🔍 Incident detected — run Knowledge Agent to investigate?",
            details=[
                f"Incident  : {event.incident_id}",
                f"Service   : {event.data.get('service', 'unknown')}",
                f"Severity  : {event.data.get('severity', 'unknown')}",
                f"Desc      : {event.data.get('description', '')}",
                f"Files     : {len(files_to_fix)} file(s) identified by log parser",
                *file_preview,
            ],
        )
        if not approved:
            logger.info("[Orchestrator] Knowledge Agent cancelled.")
            self._dash("stage", "done")
            self.print_dashboard("Investigation cancelled — no self-healing triggered")
            return

        knowledge_agent = self.registry.get_agent("knowledge_agent")
        if not knowledge_agent:
            logger.error("[Orchestrator] Knowledge Agent not registered!")
            self._dash("stage", "done")
            return

        self._dashboard["agents"]["knowledge_agent"] = "RUNNING"
        self.state_manager.set_agent_status("knowledge_agent", AgentStatus.RUNNING)
        self.print_dashboard(f"Knowledge Agent investigating incident {event.incident_id}")

        description = event.data.get("description", "")
        report      = event.data.get("report", "")
        impact      = event.data.get("impact", "")
        recommended = event.data.get("recommended", "")

        error_parts = [description]
        if report:
            error_parts.append(f"Incident report: {report}")
        if impact:
            error_parts.append(f"Impact: {impact}")
        if recommended:
            error_parts.append(f"Recommended: {recommended}")
        error_message = "\n".join(error_parts)

        try:
            extra = {
                "files_to_fix": files_to_fix,
                "impact":       impact,
                "recommended":  recommended,
                "report":       report,
            }
            agent_response = knowledge_agent.run(error_message, extra=extra)

            # ── APPROVAL 4a or 4b: based on RAG source ────────────────────
            source_value = agent_response.source.value  # "knowledge_base" or "llm_generated"

            # Try to import RAGSource for clean comparison
            try:
                from agents.knowledge_agent.shared.models import RAGSource as _RAGSource
                is_kb = (agent_response.source == _RAGSource.KNOWLEDGE_BASE)
            except Exception:
                is_kb = (source_value == "knowledge_base")

            if is_kb:
                # ── Found in Knowledge Base ────────────────────────────────
                rag = agent_response.rag_result
                approved_solution = await self.approval.request_approval(
                    title="✅ Knowledge Base match found — apply RAG solution?",
                    details=[
                        f"Source     : Knowledge Base (RAG)",
                        f"Confidence : {agent_response.confidence:.0%}",
                        f"Entry ID   : {rag.entry_id if rag else 'n/a'}",
                        f"Error      : {rag.error_pattern[:80] if rag else 'n/a'}",
                        f"Root cause : {rag.root_cause[:100] if rag else 'n/a'}",
                        f"Fix        : {agent_response.healing_prompt[:150]}",
                        *(
                            [f"Command    : {cmd}" for cmd in agent_response.suggested_commands[:3]]
                        ),
                    ],
                )
            else:
                # ── Not found in KB — LLM / web generated ─────────────────
                web_refs = [f"  [{i+1}] {url}" for i, url in enumerate(agent_response.web_sources[:3])]
                approved_solution = await self.approval.request_approval(
                    title="⚠️  No KB match — use LLM-generated solution?",
                    details=[
                        f"Source     : LLM Generated (no KB match)",
                        f"Confidence : {agent_response.confidence:.0%}",
                        f"Fix        : {agent_response.healing_prompt[:150]}",
                        *(
                            [f"Command    : {cmd}" for cmd in agent_response.suggested_commands[:3]]
                        ),
                        *(web_refs if web_refs else ["Web refs   : none"]),
                    ],
                )

            if not approved_solution:
                logger.info("[Orchestrator] Solution rejected by developer.")
                self._dash("stage", "done")
                self.print_dashboard(
                    f"Solution rejected — source={source_value}, no self-healing triggered"
                )
                return

            # ── Build Solution and publish INVESTIGATION_COMPLETE ──────────
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

            await self.event_bus.publish(Event(
                type=EventType.INVESTIGATION_COMPLETE,
                source="knowledge_agent",
                incident_id=event.incident_id,
                data={"solution": solution},
            ))

        except Exception as e:
            logger.error(f"[Orchestrator] Knowledge Agent failed: {e}", exc_info=True)
            self.print_dashboard(f"Knowledge Agent failed: {e}")
        finally:
            self._dashboard["agents"]["knowledge_agent"] = "IDLE"
            self.state_manager.set_agent_status("knowledge_agent", AgentStatus.IDLE)

    async def _on_investigation_complete(self, event: Event) -> None:
        """
        INVESTIGATION_COMPLETE handler.

        Approval flow:
          5. [APPROVAL] Apply self-healing fix?
          → SelfHealingAgent.remediate(solution)
          → publish REMEDIATION_COMPLETE or REMEDIATION_FAILED
        """
        logger.info("[Orchestrator] INVESTIGATION_COMPLETE → Self-Healing Agent")

        solution: SHSolution = event.data.get("solution")
        if not solution:
            logger.error("[Orchestrator] INVESTIGATION_COMPLETE event missing 'solution'")
            return

        # ── APPROVAL 5: apply self-healing fix? ───────────────────────────
        file_preview = [
            f"  → {f.path}:{f.line}  ({f.exception})"
            for f in solution.files_to_modify[:3]
        ]
        approved = await self.approval.request_approval(
            title="🔧 Investigation complete — apply self-healing fix?",
            details=[
                f"Root cause : {solution.root_cause[:100]}",
                f"Confidence : {solution.confidence:.0%}",
                f"Source     : {solution.source}",
                f"Files      : {len(solution.files_to_modify)} file(s) to modify",
                *file_preview,
                *(
                    [f"Command    : {cmd}" for cmd in solution.suggested_commands[:3]]
                ),
            ],
        )
        if not approved:
            logger.info("[Orchestrator] Self-Healing cancelled.")
            self._dash("stage", "done")
            self.print_dashboard("Self-healing cancelled — incident left open")
            return

        healing_agent = self.registry.get_agent("self_healing_agent")
        if not healing_agent:
            logger.error("[Orchestrator] Self-Healing Agent not registered!")
            self._dash("stage", "done")
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
            f"✅ RESOLVED — {len(files_fixed)} file(s) fixed, verification={verification}"
        )
        await self._send_alert(
            incident_id=event.incident_id,
            title="Incident Resolved",
            message=(
                f"Incident {event.incident_id} resolved automatically. "
                f"Fixed {len(files_fixed)} file(s). "
                f"Verification: {verification}."
            ),
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
            f"❌ REMEDIATION FAILED — {errors[0] if errors else 'unknown error'}"
        )
        await self._send_alert(
            incident_id=event.incident_id,
            title="Remediation Failed",
            message=(
                f"Incident {event.incident_id} could not be resolved automatically. "
                f"Errors: {errors}. Manual intervention required."
            ),
        )

    async def _send_alert(self, incident_id: str, title: str, message: str) -> None:
        alerting_agent = self.registry.get_agent("alerting_agent")
        if not alerting_agent:
            logger.warning("[Orchestrator] Alerting Agent not registered — skipping alert")
            return
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