"""
core/orchestrator.py

KEY CHANGES (vs original):
  1. __init__ accepts optional email client only (Slack removed).
  2. ApprovalManager is initialized with the email client.
  3. ApprovalServer (HTTP) is created and started/stopped via
     start_approval_server() / stop_approval_server() — called by devops.py.
  4. _send_alert() routes to email only; urgent=True (HIGH/CRITICAL severity)
     activates the emergency lane which broadcasts to the full EMAIL_TEAM list.
  5. _pause_monitoring / _resume_monitoring removed from orchestrator body —
     they now live inside ApprovalManager, which calls them automatically.
"""

import asyncio
import logging
import sys
import pathlib
from datetime import datetime
from typing import Dict, List, Optional

from core.models import (
    AgentStatus,
    Incident,
    IncidentStatus,
    RemediationStatus,
    Solution,
)
from core.event_bus      import EventBus, Event, EventType
from core.state_manager  import StateManager
from core.context_manager import ContextManager
from core.agent_registery import AgentRegistry
from core.approval_manager import ApprovalManager
from core.approval_server  import ApprovalServer

# Optional — only imported if the email client object is provided
# (avoids hard dependency when Email is not configured)
try:
    from core.email_client import EmailClient as _EmailClient
except ImportError:
    _EmailClient = None

from agents.self_healing_agent.models import FileToFix, Solution as SHSolution
from agents.monitoring_agent.agent    import MonitoringAgent
from agents.monitoring_agent.config   import MonitoringConfig

try:
    from agents.knowledge_agent.shared.models import RAGSource
except Exception:
    RAGSource = None

logger = logging.getLogger(__name__)


class Orchestrator:

    # ── Embedded seed documents ────────────────────────────────────────────────
    _SEED_DOCUMENTS = [
        {
            "id": 1,
            "text": (
                "SQLAlchemy connection pool exhaustion fix: set pool_size=20 and "
                "max_overflow=10 on create_engine(). The default pool_size=5 is too "
                "small for production workloads. Also set pool_pre_ping=True so stale "
                "connections are recycled automatically."
            ),
            "source": "runbook/sqlalchemy",
            "tags": ["sqlalchemy", "connection-pool", "python"],
        },
        {
            "id": 2,
            "text": (
                "OperationalError: QueuePool limit overflow. Symptoms: requests hang or "
                "raise 'TimeoutError: QueuePool limit of size X overflow Y reached'. "
                "Root cause: pool_size too small or connections not closed. Fix: increase "
                "pool_size, ensure every engine.connect() is used as a context manager, "
                "and never silence exceptions with a bare except clause."
            ),
            "source": "runbook/sqlalchemy",
            "tags": ["sqlalchemy", "connection-pool", "error"],
        },
        {
            "id": 3,
            "text": (
                "Python bare except anti-pattern: except: catches BaseException including "
                "KeyboardInterrupt and SystemExit, hiding all errors silently. Always catch "
                "specific exceptions, e.g. except sqlalchemy.exc.OperationalError as e "
                "and log or re-raise."
            ),
            "source": "runbook/python-best-practices",
            "tags": ["python", "exception-handling"],
        },
        {
            "id": 4,
            "text": (
                "PostgreSQL max_connections default is 100. Each SQLAlchemy pool_size slot "
                "holds one persistent connection. If multiple services share the same DB, "
                "use PgBouncer as a connection pooler to avoid hitting the server limit."
            ),
            "source": "runbook/postgresql",
            "tags": ["postgresql", "connection-pool"],
        },
        {
            "id": 5,
            "text": (
                "Pod CrashLoopBackOff caused by OOMKilled: increase memory limits in the "
                "Deployment manifest. Check kubectl describe pod name for Last State "
                "exit code 137 (OOM). Typical fix: set resources.limits.memory to at least "
                "512Mi for Python services."
            ),
            "source": "runbook/kubernetes",
            "tags": ["kubernetes", "oom", "crashloop"],
        },
        {
            "id": 6,
            "text": (
                "Kubernetes liveness probe failing: service starts slowly and probe fires "
                "before the app is ready. Fix: add initialDelaySeconds=30 to the "
                "livenessProbe spec, or switch to a startupProbe for slow-starting containers."
            ),
            "source": "runbook/kubernetes",
            "tags": ["kubernetes", "liveness-probe"],
        },
        {
            "id": 7,
            "text": (
                "Redis NOAUTH Authentication required: the client is connecting without a "
                "password but requirepass is set in redis.conf. Fix: pass the password in "
                "the connection URL redis://:password@host:6379 or set REDIS_PASSWORD env var."
            ),
            "source": "runbook/redis",
            "tags": ["redis", "auth"],
        },
        {
            "id": 8,
            "text": (
                "Redis connection timeout in high-traffic services: increase the connection "
                "pool size in the client library (e.g. redis-py: ConnectionPool(max_connections=50)). "
                "Also enable TCP keepalive to detect dead connections early."
            ),
            "source": "runbook/redis",
            "tags": ["redis", "connection-pool", "timeout"],
        },
        {
            "id": 9,
            "text": (
                "HTTP 503 Service Unavailable from downstream API: implement exponential "
                "backoff with jitter (initial=0.5s, max=30s, multiplier=2). Use a circuit "
                "breaker (e.g. pybreaker) to stop cascading failures when the downstream "
                "is consistently unavailable."
            ),
            "source": "runbook/microservices",
            "tags": ["http", "retry", "circuit-breaker"],
        },
        {
            "id": 10,
            "text": (
                "High CPU usage in Python service: profile with py-spy top --pid pid. "
                "Common culprits: N+1 database queries (fix with eager loading), unbounded "
                "loops, or missing indexes. Use EXPLAIN ANALYZE in PostgreSQL to find slow queries."
            ),
            "source": "runbook/performance",
            "tags": ["performance", "cpu", "python"],
        },
        {
            "id": 11,
            "text": (
                "Docker login-action failure in GitHub Actions: DOCKER_USERNAME or DOCKER_PASSWORD "
                "secret is missing or wrong. Go to repo Settings > Secrets > Actions and add "
                "DOCKER_USERNAME and DOCKER_PASSWORD. The password should be a Docker Hub access "
                "token, not your account password."
            ),
            "source": "runbook/github-actions",
            "tags": ["docker", "github-actions", "cicd", "auth"],
        },
        {
            "id": 12,
            "text": (
                "GitHub Actions build fails at docker/login-action: conclusion=failure means "
                "authentication to Docker Hub failed. Steps to fix: 1) Create a Docker Hub "
                "access token at hub.docker.com > Account Settings > Security. "
                "2) Add it as DOCKER_PASSWORD secret in GitHub repo settings. "
                "3) Add your Docker Hub username as DOCKER_USERNAME secret."
            ),
            "source": "runbook/github-actions",
            "tags": ["docker", "github-actions", "login", "secret"],
        },
        {
            "id": 13,
            "text": (
                "CI/CD pipeline docker push fails with unauthorized: authentication required. "
                "Root cause: missing or expired Docker Hub credentials in GitHub secrets. "
                "Fix: regenerate Docker Hub token and update DOCKER_PASSWORD secret. "
                "Verify DOCKER_USERNAME matches your Docker Hub account exactly."
            ),
            "source": "runbook/cicd",
            "tags": ["docker", "push", "unauthorized", "cicd"],
        },
    ]

    # ── Init ──────────────────────────────────────────────────────────────────

    def __init__(
        self,
        email=None,    # core.email_client.EmailClient  — optional
    ):
        self.event_bus       = EventBus()
        self.state_manager   = StateManager()
        self.context_manager = ContextManager()
        self.registry        = AgentRegistry()

        # Store email client for _send_alert()
        self._email = email

        # ── Approval stack ────────────────────────────────────────────
        self.approval = ApprovalManager(
            email           = email,
            timeout_seconds = 300,
            registry        = self.registry,
        )

        # HTTP server for email link clicks (approve/deny).
        # Created here; started/stopped by devops.py around the pipeline.
        self._approval_server: Optional[ApprovalServer] = (
            ApprovalServer(
                approval_manager = self.approval,
                email_client     = email,
            )
            if email
            else None
        )

        self._running = False

        # ── per-incident retry tracking ───────────────────────────────
        self._failed_solutions: Dict[str, List[str]] = {}

        self._subscribe_to_events()
        self._register_monitoring_agent()
        self._register_knowledge_agent()
        self._register_self_healing_agent()

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

    # ── Approval server lifecycle (called by devops.py) ───────────────────────

    async def start_approval_server(self) -> None:
        """Start the HTTP server that receives email link clicks (approve/deny)."""
        if self._approval_server:
            url = await self._approval_server.start()
            logger.info("[Orchestrator] ApprovalServer started at %s", url)
            if url:
                print(f"\n  [ApprovalServer] Listening at: {url}")
                print(f"  [ApprovalServer] Email approve/deny links will use this base URL.\n")

    async def stop_approval_server(self) -> None:
        """Stop the HTTP server and close any ngrok tunnel."""
        if self._approval_server:
            await self._approval_server.stop()

    # ── Event subscriptions ───────────────────────────────────────────────────

    def _subscribe_to_events(self) -> None:
        self.event_bus.subscribe(EventType.SCAFFOLD_STARTED,        self._on_scaffold_started)
        self.event_bus.subscribe(EventType.SCAFFOLD_COMPLETE,       self._on_scaffold_complete)
        self.event_bus.subscribe(EventType.SCAFFOLD_FAILED,         self._on_scaffold_failed)
        self.event_bus.subscribe(EventType.INCIDENT_CREATED,        self._on_incident_created)
        self.event_bus.subscribe(EventType.INVESTIGATION_COMPLETE,  self._on_investigation_complete)
        self.event_bus.subscribe(EventType.REMEDIATION_COMPLETE,    self._on_remediation_complete)
        self.event_bus.subscribe(EventType.REMEDIATION_FAILED,      self._on_remediation_failed)

    def register_agent(self, name: str, agent: object, metadata: Optional[dict] = None) -> None:
        self.registry.register(name, agent, metadata)
        self.state_manager.set_agent_status(name, AgentStatus.IDLE)
        logger.info(f"[Orchestrator] Agent registered: '{name}'")

    # ── Agent auto-registration ───────────────────────────────────────────────

    def _register_monitoring_agent(self) -> None:
        if self.registry.get_agent("monitoring_agent"):
            return
        config = MonitoringConfig(collector_backend="file", log_dir="logs")
        agent  = MonitoringAgent(
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
            return
        if getattr(agent, "_poll_task", None) and not agent._poll_task.done():
            return
        await agent.start()

    def _register_knowledge_agent(self) -> None:
        if self.registry.get_agent("knowledge_agent"):
            return

        try:
            _project_root = pathlib.Path(__file__).resolve().parents[1]
            if str(_project_root) not in sys.path:
                sys.path.insert(0, str(_project_root))

            _stale = [
                k for k in sys.modules
                if k in ("shared", "knowledge_core")
                or k.startswith("shared.")
                or k.startswith("knowledge_core.")
                or k.startswith("agents.knowledge_agent")
            ]
            for k in _stale:
                del sys.modules[k]

            _ka_root = _project_root / "agents" / "knowledge_agent"
            if str(_ka_root) not in sys.path:
                sys.path.insert(0, str(_ka_root))

            try:
                from qdrant_client import QdrantClient
                from qdrant_client.models import Distance, VectorParams, PointStruct
                from sentence_transformers import SentenceTransformer
                from agents.knowledge_agent.shared.config import load_config as _ka_cfg

                _cfg        = _ka_cfg()
                _client     = QdrantClient(host=_cfg.qdrant_host, port=_cfg.qdrant_port)
                _collection = _cfg.collection_name

                _populated = False
                try:
                    _count = _client.count(collection_name=_collection).count
                    if _count > 0:
                        _populated = True
                        logger.info(
                            "[Orchestrator] Qdrant '%s' already has %d vectors — skipping seed",
                            _collection, _count,
                        )
                except Exception:
                    pass

                if not _populated:
                    logger.info("[Orchestrator] Seeding Qdrant '%s' (first run)...", _collection)
                    print(f"\n  [Knowledge Base] Seeding Qdrant '{_collection}'...")

                    existing = [c.name for c in _client.get_collections().collections]
                    if _collection in existing:
                        _client.delete_collection(_collection)
                    _client.create_collection(
                        collection_name=_collection,
                        vectors_config=VectorParams(size=384, distance=Distance.COSINE),
                    )

                    _model  = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
                    _points = []
                    for doc in self._SEED_DOCUMENTS:
                        _vec = _model.encode(doc["text"]).tolist()
                        _points.append(PointStruct(
                            id      = doc["id"],
                            vector  = _vec,
                            payload = {
                                "text"  : doc["text"],
                                "source": doc["source"],
                                "tags"  : doc["tags"],
                            },
                        ))
                    _client.upsert(collection_name=_collection, points=_points)

                    try:
                        from agents.knowledge_agent.ingestion.pipeline import run_pipeline
                        run_pipeline()
                        logger.info("[Orchestrator] Full ingestion pipeline complete")
                    except Exception as _pe:
                        logger.info(
                            "[Orchestrator] Full pipeline skipped (%s) — seed docs loaded OK", _pe
                        )

                    _final = _client.count(collection_name=_collection).count
                    logger.info(
                        "[Orchestrator] Qdrant seeded — %d vectors in '%s'", _final, _collection
                    )
                    print(f"  [Knowledge Base] Seeded {_final} vectors into '{_collection}'\n")

            except Exception as _seed_err:
                logger.warning(
                    "[Orchestrator] Qdrant seed skipped: %s — is Qdrant running at localhost:6333?",
                    _seed_err,
                )
                print(f"  [Knowledge Base] Qdrant unavailable ({_seed_err}) — LLM fallback active\n")

            _stale2 = [k for k in sys.modules if k == "shared" or k.startswith("shared.")]
            for k in _stale2:
                del sys.modules[k]

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
        if self.registry.get_agent("self_healing_agent"):
            return
        try:
            from agents.self_healing_agent.self_healing_agent import SelfHealingAgent
            import os
            project_root = os.getcwd()
            agent = SelfHealingAgent(apply_changes=True, project_root=project_root)
            self.registry.register("self_healing_agent", agent)
            self.state_manager.set_agent_status("self_healing_agent", AgentStatus.IDLE)
            logger.info(
                "[Orchestrator] SelfHealingAgent registered — "
                "backups → %s/.self_healing_backups/", project_root
            )
        except Exception as exc:
            logger.error("[Orchestrator] Failed to register SelfHealingAgent: %s", exc)

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
            elif raw_status == "RUNNING":
                col, sym = YL, "▶"
            elif raw_status == "IDLE":
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

    # Kept as stubs — ApprovalManager now calls these internally
    def _pause_monitoring(self) -> None:
        agent = self.registry.get_agent("monitoring_agent")
        if agent and hasattr(agent, "pause"):
            agent.pause()

    def _resume_monitoring(self) -> None:
        agent = self.registry.get_agent("monitoring_agent")
        if agent and hasattr(agent, "resume"):
            agent.resume()

    # ── Scaffold ──────────────────────────────────────────────────────────────

    async def run_scaffold(self, project_path: str, dry_run: bool = False, skip_scaffold: bool = False) -> None:
        logger.info(f"[Orchestrator] run_scaffold: {project_path}")
        await self.event_bus.publish(Event(
            type=EventType.SCAFFOLD_STARTED,
            source="cli",
            data={"project_path": project_path, "dry_run": dry_run, "skip_scaffold": skip_scaffold},
        ))

    async def _on_scaffold_started(self, event: Event) -> None:
        self._dash("stage",   "scaffold")
        self._dash("project", event.data.get("project_path", ""))
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
        project_path   = event.data.get("project_path")
        dry_run        = event.data.get("dry_run", False)
        skip_scaffold  = event.data.get("skip_scaffold", False)

        # ── skip scaffold — files already exist, user said no to re-generate ──
        if skip_scaffold:
            import os
            existing_files = []
            for f in ["Dockerfile", "docker-compose.yml", ".dockerignore",
                      ".github/workflows/deploy.yml",
                      "k8s/deployment.yaml", "k8s/service.yaml", "k8s/ingress.yaml"]:
                if os.path.exists(os.path.join(project_path, f)):
                    existing_files.append(f)

            self.state_manager.set_agent_status("scaffold_agent", AgentStatus.IDLE)
            self._dashboard["agents"]["scaffold_agent"] = "IDLE"
            self.print_dashboard(f"Scaffold skipped — {len(existing_files)} existing files reused")

            await self.event_bus.publish(Event(
                type=EventType.SCAFFOLD_COMPLETE,
                source="scaffold_agent",
                data={
                    "project_path"   : project_path,
                    "dry_run"        : dry_run,
                    "language"       : "unknown",
                    "framework"      : "unknown",
                    "generated_files": existing_files,
                    "skipped"        : True,
                },
            ))
            return

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
                    "skipped"        : False,
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

        if dry_run:
            self._dash("stage", "done")
            self.print_dashboard("Dry-run complete — no CI/CD triggered")
            return

        # ── GATE 1: Scaffold complete → proceed to CI/CD? ─────────────────
        approved = await self.approval.request_approval(
            title=f"Scaffold complete — {framework} ({language}). Proceed to CI/CD?",
            details=files,
            context={"project_path": project_path},
        )
        if not approved:
            print("\n  Pipeline stopped.\n")
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
            # Push failed — ask developer if they want to continue anyway
            # (GitHub Actions from a previous push might already be running)
            self.print_dashboard("Git push failed — checking if we should continue to CI/CD")
            self._pause_monitoring()
            print(f"\n{'─'*55}")
            print(f"  Git push failed (see error above).")
            print(f"  GitHub Actions from a previous push may still be running.")
            print(f"{'─'*55}")
            try:
                cont = input("  Continue to CI/CD anyway? [yes/no]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                cont = "no"
            self._resume_monitoring()
            if cont not in ("yes", "y"):
                self._dash("stage", "done")
                return
            print("  Continuing to CI/CD...")

        self._dash("repo_url", repo_url)

        cicd_agent = self.registry.get_agent("cicd_agent")
        if not cicd_agent:
            self._dash("stage", "cicd")
            self._dash("cicd_status", "triggered (no log collection)")
            self.print_dashboard("GitHub Actions triggered by push")
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

        run  = None
        logs = []

        try:
            repo = repo_url.replace("https://github.com/", "").replace(".git", "")
            print("  Waiting for GitHub Actions to start...")
            await asyncio.sleep(10)

            run = await self._get_latest_run(cicd_agent, repo, token)

            if not run:
                self._dash("cicd_status", "no run found")
                self.print_dashboard("Could not find pipeline run")
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
                    f"CI/CD finished — {run.status.upper()}, {len(logs)} log lines"
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

            # Send deployment alert (one-way notification, not an approval)
            pipeline_status = run.status if run else "unknown"
            await self._send_alert(
                incident_id = "deployment",
                title       = f"CI/CD {pipeline_status.upper()} — {project_path}",
                message     = (
                    f"Pipeline finished with status: {pipeline_status}\n"
                    f"Repo: {repo_url}\n"
                    f"Logs: {len(logs)} lines collected"
                ),
            )

            # ── GATE 2: CI/CD done → run Monitoring Agent? ────────────────
            approved = await self.approval.request_approval(
                title=f"CI/CD {pipeline_status.upper()} — run Monitoring Agent to analyze?",
                details=logs[:10] if logs else [
                    f"Pipeline status : {pipeline_status}",
                    f"Pipeline URL    : {run.url if run else 'N/A'}",
                    "No logs collected — pipeline may have failed early",
                ],
                context={"project_path": project_path},
            )
            if not approved:
                print("\n  Monitoring skipped.\n")
                self._dash("stage", "done")
                return

            monitoring_agent = self.registry.get_agent("monitoring_agent")
            if not monitoring_agent:
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
                    self.print_dashboard("Monitoring complete — no incidents detected")
            finally:
                self._dashboard["agents"]["monitoring_agent"] = "IDLE"
                self.state_manager.set_agent_status("monitoring_agent", AgentStatus.IDLE)

        except Exception as e:
            logger.error(f"[Orchestrator] CI/CD Agent failed: {e}")
            self._dash("cicd_status", f"error: {e}")
            self.print_dashboard(f"CI/CD Agent error: {e}")
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
                                return await cicd_agent.get_pipeline_status(str(runs[0]["id"]), repo)
            except Exception as e:
                logger.warning(f"[Orchestrator] get_latest_run attempt {attempt+1}: {e}")
            if attempt < 3:
                print(f"  Waiting for run to appear... (attempt {attempt+2}/4)")
                await asyncio.sleep(8)
        return None

    async def _on_scaffold_failed(self, event: Event) -> None:
        err = event.data.get("error", "unknown")
        self._dash("stage", "done")
        self.print_dashboard(f"Scaffold FAILED: {err}")

    async def _push_to_github(self, project_path: str, repo_url: str, token: str) -> bool:
        import subprocess
        auth_url = repo_url.replace("https://", f"https://{token}@") if repo_url.startswith("https://") else repo_url

        def run(cmd, cwd):
            r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
            return r.returncode, r.stdout + r.stderr

        _SENSITIVE = [
            ".env", ".env.*", "*.env",
            ".devops_llm_config", ".devops_state",
            "*.key", "*.pem", "*.p12",
            "__pycache__/", "*.pyc", "*.pyo",
            ".vscode/", ".idea/",
        ]

        # ── Step 1: git init ──────────────────────────────────────────────
        run(["git", "init"], project_path)
        print("  [git init] OK")

        # ── Step 2: Write .gitignore BEFORE git add ───────────────────────
        gitignore_path = pathlib.Path(project_path) / ".gitignore"
        try:
            existing = gitignore_path.read_text(encoding="utf-8") if gitignore_path.exists() else ""
            lines    = existing.splitlines()
            added    = [e for e in _SENSITIVE if e not in lines]
            lines.extend(added)
            gitignore_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            if added:
                print(f"  [gitignore] Protected: {', '.join(added)}")
        except Exception as e:
            logger.warning(f"[Orchestrator] Could not update .gitignore: {e}")

        # ── Step 3: Physically delete sensitive files from project dir ────
        # The real fix for GitHub Push Protection: if .devops_llm_config or
        # .env exist inside the project folder being pushed, delete them so
        # they can never appear in the git history — even if .gitignore fails.
        _delete_from_project = [".devops_llm_config", ".devops_state"]
        p = pathlib.Path(project_path)
        for fname in _delete_from_project:
            fpath = p / fname
            if fpath.exists():
                try:
                    fpath.unlink()
                    print(f"  [protected] Deleted {fname} from project folder (safe — stored in SDK root)")
                except Exception:
                    pass

        # ── Step 4: Untrack any sensitive files from git index ────────────
        _untrack = [".env", ".devops_llm_config", ".devops_state"]
        for f in _untrack:
            run(["git", "rm", "--cached", "-f", f], project_path)
            run(["git", "rm", "-rf", "--cached", f], project_path)

        # ── Step 5: Clean git history if previous bad commits exist ──────
        # Squash all previous commits into one clean commit to remove
        # any secrets that were committed in prior runs.
        code, _ = run(["git", "log", "--oneline", "-1"], project_path)
        if code == 0:
            # History exists — orphan reset to remove all previous commits
            run(["git", "checkout", "--orphan", "clean_main"], project_path)
            run(["git", "add", "."], project_path)
            code2, _ = run(
                ["git", "commit", "-m", "chore: clean deployment (no secrets)"],
                project_path,
            )
            if code2 == 0:
                run(["git", "branch", "-D", "main"], project_path)
                run(["git", "branch", "-m", "main"], project_path)
                print("  [git history] Cleaned — fresh commit with no secrets")

        # ── Step 6: Stage, commit (if needed), push ───────────────────────
        print(f"\n  Pushing to: {repo_url}")
        _OPTIONAL = {"remove old remote (ok if fails)", "git commit"}
        steps = [
            (["git", "add", "."],                                 "git add"),
            (["git", "commit", "-m", "chore: add DevOps scaffold files"], "git commit"),
            (["git", "branch", "-M", "main"],                    "git branch"),
            (["git", "remote", "remove", "origin"],              "remove old remote (ok if fails)"),
            (["git", "remote", "add", "origin", auth_url],       "git remote add"),
            (["git", "push", "-u", "origin", "main", "--force"], "git push"),
        ]
        for cmd, label in steps:
            code, out = run(cmd, project_path)
            if code != 0 and label not in _OPTIONAL:
                print(f"\n  Failed at [{label}]:")
                print(f"  {out.strip()[:500]}")
                if "push protection" in out.lower() or "GH013" in out:
                    print()
                    print("  GITHUB PUSH PROTECTION still blocked.")
                    print("  The repo has old commits with secrets in its history.")
                    print("  Fix: go to GitHub → repo → Settings → Code security")
                    print("       → Secret scanning → bypass the block, OR")
                    print("       delete the repo and create a new empty one.")
                return False
            print(f"  [{label}] OK")
        print(f"  Pushed successfully to {repo_url}\n")
        return True

    # ── Incident Workflow ─────────────────────────────────────────────────────

    async def handle_incident(self, incident: Incident) -> None:
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
        files_to_fix: list = event.data.get("files_to_fix", [])
        file_preview = [f"  -> {f.get('file','?')}:{f.get('line','?')}" for f in files_to_fix[:3]]

        # ── GATE 3: Incident detected → run Knowledge Agent? ──────────────
        approved = await self.approval.request_approval(
            title="Incident detected — run Knowledge Agent to investigate?",
            details=[
                f"Incident  : {event.incident_id}",
                f"Service   : {event.data.get('service', 'unknown')}",
                f"Severity  : {event.data.get('severity', 'unknown')}",
                f"Desc      : {event.data.get('description', '')}",
                f"Files     : {len(files_to_fix)} file(s) identified",
                *file_preview,
            ],
        )
        if not approved:
            self._dash("stage", "done")
            self.print_dashboard("Investigation cancelled")
            return

        knowledge_agent = self.registry.get_agent("knowledge_agent")
        if not knowledge_agent:
            logger.error("[Orchestrator] Knowledge Agent not registered!")
            self._dash("stage", "done")
            return

        self._dashboard["agents"]["knowledge_agent"] = "RUNNING"
        self.state_manager.set_agent_status("knowledge_agent", AgentStatus.RUNNING)
        self.print_dashboard(f"Knowledge Agent investigating {event.incident_id}")

        description = event.data.get("description", "")
        report      = event.data.get("report", "")
        impact      = event.data.get("impact", "")
        recommended = event.data.get("recommended", "")

        error_parts = [description]
        if report:      error_parts.append(f"Incident report: {report}")
        if impact:      error_parts.append(f"Impact: {impact}")
        if recommended: error_parts.append(f"Recommended: {recommended}")
        error_message = "\n".join(error_parts)

        try:
            failed_solutions = self._failed_solutions.get(event.incident_id, [])
            extra = {
                "files_to_fix"    : files_to_fix,
                "impact"          : impact,
                "recommended"     : recommended,
                "report"          : report,
                "failed_solutions": failed_solutions,
            }
            agent_response = knowledge_agent.run(error_message, extra=extra)

            source_value = agent_response.source.value
            try:
                from agents.knowledge_agent.shared.models import RAGSource as _RAGSource
                is_kb = (agent_response.source == _RAGSource.KNOWLEDGE_BASE)
            except Exception:
                is_kb = (source_value == "knowledge_base")

            if is_kb:
                rag = agent_response.rag_result
                # ── GATE 4a: KB match found → apply RAG solution? ─────────
                approved_solution = await self.approval.request_approval(
                    title="Knowledge Base match found — apply RAG solution?",
                    details=[
                        f"Source     : Knowledge Base (RAG)",
                        f"Confidence : {agent_response.confidence:.0%}",
                        f"Entry ID   : {rag.entry_id if rag else 'n/a'}",
                        f"Root cause : {rag.root_cause[:100] if rag else 'n/a'}",
                        f"Fix        : {agent_response.healing_prompt[:150]}",
                        *[f"Command    : {cmd}" for cmd in agent_response.suggested_commands[:3]],
                    ],
                )
            else:
                web_refs = [f"  [{i+1}] {url}" for i, url in enumerate(agent_response.web_sources[:3])]
                # ── GATE 4b: No KB match → use LLM solution? ─────────────
                approved_solution = await self.approval.request_approval(
                    title="No KB match — use LLM-generated solution?",
                    details=[
                        f"Source     : LLM Generated (no KB match)",
                        f"Confidence : {agent_response.confidence:.0%}",
                        f"Fix        : {agent_response.healing_prompt[:150]}",
                        *[f"Command    : {cmd}" for cmd in agent_response.suggested_commands[:3]],
                        *(web_refs if web_refs else ["Web refs   : none"]),
                    ],
                )

            if not approved_solution:
                self._dash("stage", "done")
                self.print_dashboard("Solution rejected — no self-healing triggered")
                return

            files_to_modify = [
                FileToFix.from_monitoring(entry)
                for entry in files_to_fix
                if entry.get("file") or entry.get("path")
            ]

            solution = SHSolution(
                incident_id        = event.incident_id,
                root_cause         = getattr(agent_response.rag_result, "root_cause", "") or error_message,
                healing_prompt     = agent_response.healing_prompt,
                confidence         = agent_response.confidence,
                suggested_commands = agent_response.suggested_commands,
                references         = list(agent_response.web_sources),
                source             = agent_response.source.value,
                files_to_modify    = files_to_modify,
            )

            self.state_manager.add_solution(solution)
            self.print_dashboard(
                f"Knowledge Agent complete — source={solution.source} "
                f"confidence={solution.confidence:.0%}"
            )

            await self.event_bus.publish(Event(
                type=EventType.INVESTIGATION_COMPLETE,
                source="knowledge_agent",
                incident_id=event.incident_id,
                data={
                    "solution"     : solution,
                    "error_message": error_message,
                    "files_to_fix" : files_to_fix,
                },
            ))

        except Exception as e:
            logger.error(f"[Orchestrator] Knowledge Agent failed: {e}", exc_info=True)
            self.print_dashboard(f"Knowledge Agent failed: {e}")
        finally:
            self._dashboard["agents"]["knowledge_agent"] = "IDLE"
            self.state_manager.set_agent_status("knowledge_agent", AgentStatus.IDLE)

    # ── Investigation Complete → Self-Healing + Retry Loop ───────────────────

    async def _on_investigation_complete(self, event: Event) -> None:
        solution: SHSolution = event.data.get("solution")
        if not solution:
            return

        error_message : str  = event.data.get("error_message", solution.root_cause)
        files_to_fix  : list = event.data.get("files_to_fix", [])
        retry_count          = len(self._failed_solutions.get(event.incident_id, []))

        file_preview = [
            f"  -> {f.path}:{f.line}  ({f.exception})"
            for f in solution.files_to_modify[:3]
        ]

        # ── GATE 5: No-files → generate instructions? ─────────────────────
        if not solution.files_to_modify:
            approved = await self.approval.request_approval(
                title="No files to auto-fix — generate manual instructions?",
                details=[
                    f"Root cause : {solution.root_cause[:100]}",
                    f"Confidence : {solution.confidence:.0%}",
                    f"Source     : {solution.source}",
                    "The agent will produce step-by-step manual instructions.",
                ],
            )
        else:
            # ── GATE 5: Files found → apply self-healing fix? ─────────────
            retry_label = f" (Retry #{retry_count})" if retry_count > 0 else ""
            approved = await self.approval.request_approval(
                title=f"Investigation complete{retry_label} — apply self-healing fix?",
                details=[
                    f"Root cause : {solution.root_cause[:100]}",
                    f"Confidence : {solution.confidence:.0%}",
                    f"Source     : {solution.source}",
                    f"Files      : {len(solution.files_to_modify)} file(s) to modify",
                    *file_preview,
                    *[f"Command    : {cmd}" for cmd in solution.suggested_commands[:3]],
                ],
            )

        if not approved:
            self._dash("stage", "done")
            self.print_dashboard("Self-healing cancelled")
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
        self.print_dashboard(
            f"Self-Healing Agent applying fix — "
            f"{len(solution.files_to_modify)} file(s) (attempt #{retry_count + 1})"
        )

        try:
            result = await healing_agent.remediate(solution, retry_count=retry_count)

            # ── INSTRUCTIONS_ONLY ─────────────────────────────────────────
            if result.status == RemediationStatus.INSTRUCTIONS_ONLY:
                self._dash("stage", "done")
                self._track_incident(event.incident_id, "", "", "", "MANUAL_REQUIRED")
                print(f"\n{'═' * 66}")
                print(f"  MANUAL ACTION REQUIRED — {event.incident_id}")
                print(f"{'─' * 66}")
                print(result.instructions)
                print(f"{'═' * 66}\n")
                self.print_dashboard("No auto-fix available — manual instructions printed above")
                await self._send_alert(
                    incident_id = event.incident_id,
                    title       = "Manual Action Required",
                    message     = (
                        f"Incident {event.incident_id} requires manual intervention.\n"
                        f"{result.instructions}"
                    ),
                )
                return

            # ── SUCCESS ───────────────────────────────────────────────────
            if result.status == RemediationStatus.SUCCESS:
                if retry_count > 0:
                    knowledge_agent = self.registry.get_agent("knowledge_agent")
                    if knowledge_agent and hasattr(knowledge_agent, "add_to_kb"):
                        knowledge_agent.add_to_kb(
                            incident_id    = event.incident_id,
                            root_cause     = solution.root_cause,
                            healing_prompt = solution.healing_prompt,
                            commands       = solution.suggested_commands,
                            tags           = [],
                        )

                self._failed_solutions.pop(event.incident_id, None)

                await self.event_bus.publish(Event(
                    type=EventType.REMEDIATION_COMPLETE,
                    source="self_healing_agent",
                    incident_id=event.incident_id,
                    data={
                        "status"      : result.status.value,
                        "files_fixed" : [m.path for m in result.file_modifications if m.applied],
                        "confidence"  : result.confidence,
                        "verification": result.verification.status.value if result.verification else "not_run",
                    },
                ))
                return

            # ── ROLLED_BACK: retry with a different solution ──────────────
            if result.status == RemediationStatus.ROLLED_BACK:
                ver_reason = (
                    result.verification.reason
                    if result.verification else "verification failed"
                )
                self.print_dashboard(
                    f"Fix rolled back — verification failed: {ver_reason[:80]}"
                )
                print(
                    f"\n  [Self-Healing] Attempt #{retry_count + 1} rolled back.\n"
                    f"  Reason: {ver_reason}\n"
                    f"  Requesting a new solution from the Knowledge Agent...\n"
                )

                if event.incident_id not in self._failed_solutions:
                    self._failed_solutions[event.incident_id] = []
                self._failed_solutions[event.incident_id].append(
                    solution.healing_prompt[:300]
                )

                # ── GATE 6: Rolled back → try different solution? ─────────
                retry_approved = await self.approval.request_approval(
                    title=(
                        f"Fix rolled back (attempt #{retry_count + 1}) — "
                        f"try a different solution?"
                    ),
                    details=[
                        f"Incident   : {event.incident_id}",
                        f"Reason     : {ver_reason[:120]}",
                        f"Attempts   : {retry_count + 1} so far",
                        "The Knowledge Agent will suggest a DIFFERENT fix.",
                        "Choose 'no' to stop and handle manually.",
                    ],
                )

                if not retry_approved:
                    self._dash("stage", "done")
                    self.print_dashboard("Retry cancelled — manual intervention required")
                    await self.event_bus.publish(Event(
                        type=EventType.REMEDIATION_FAILED,
                        source="orchestrator",
                        incident_id=event.incident_id,
                        data={
                            "status": RemediationStatus.ROLLED_BACK.value,
                            "errors": [f"All {retry_count + 1} attempts rolled back. Manual fix required."],
                        },
                    ))
                    return

                knowledge_agent = self.registry.get_agent("knowledge_agent")
                if not knowledge_agent:
                    logger.error("[Orchestrator] Knowledge Agent not registered for retry!")
                    await self.event_bus.publish(Event(
                        type=EventType.REMEDIATION_FAILED,
                        source="orchestrator",
                        incident_id=event.incident_id,
                        data={
                            "status": RemediationStatus.FAILED.value,
                            "errors": ["Knowledge Agent unavailable for retry."],
                        },
                    ))
                    return

                self._dashboard["agents"]["knowledge_agent"] = "RUNNING"
                self.state_manager.set_agent_status("knowledge_agent", AgentStatus.RUNNING)
                self.print_dashboard(
                    f"Knowledge Agent re-investigating {event.incident_id} "
                    f"(attempt #{retry_count + 2})"
                )

                try:
                    failed_solutions = self._failed_solutions.get(event.incident_id, [])
                    extra = {
                        "files_to_fix"    : files_to_fix,
                        "failed_solutions": failed_solutions,
                    }
                    new_response = knowledge_agent.run(error_message, extra=extra)

                    # ── GATE 4 again: approve new solution before applying ─
                    new_approved = await self.approval.request_approval(
                        title=f"New solution found (attempt #{retry_count + 2}) — apply?",
                        details=[
                            f"Source     : {new_response.source.value}",
                            f"Confidence : {new_response.confidence:.0%}",
                            f"Fix        : {new_response.healing_prompt[:150]}",
                            *[f"Command    : {cmd}" for cmd in new_response.suggested_commands[:3]],
                        ],
                    )

                    if not new_approved:
                        self._dash("stage", "done")
                        self.print_dashboard("New solution rejected — stopping")
                        await self.event_bus.publish(Event(
                            type=EventType.REMEDIATION_FAILED,
                            source="orchestrator",
                            incident_id=event.incident_id,
                            data={
                                "status": RemediationStatus.FAILED.value,
                                "errors": ["User rejected retry solution."],
                            },
                        ))
                        return

                    new_files_to_modify = [
                        FileToFix.from_monitoring(entry)
                        for entry in files_to_fix
                        if entry.get("file") or entry.get("path")
                    ]

                    new_solution = SHSolution(
                        incident_id        = event.incident_id,
                        root_cause         = getattr(new_response.rag_result, "root_cause", "") or error_message,
                        healing_prompt     = new_response.healing_prompt,
                        confidence         = new_response.confidence,
                        suggested_commands = new_response.suggested_commands,
                        references         = list(new_response.web_sources),
                        source             = new_response.source.value,
                        files_to_modify    = new_files_to_modify,
                    )

                    await self.event_bus.publish(Event(
                        type=EventType.INVESTIGATION_COMPLETE,
                        source="knowledge_agent",
                        incident_id=event.incident_id,
                        data={
                            "solution"     : new_solution,
                            "error_message": error_message,
                            "files_to_fix" : files_to_fix,
                        },
                    ))

                except Exception as e:
                    logger.error(f"[Orchestrator] Knowledge Agent retry failed: {e}", exc_info=True)
                    self.print_dashboard(f"Knowledge Agent retry failed: {e}")
                    await self.event_bus.publish(Event(
                        type=EventType.REMEDIATION_FAILED,
                        source="orchestrator",
                        incident_id=event.incident_id,
                        data={"status": RemediationStatus.FAILED.value, "errors": [str(e)]},
                    ))
                finally:
                    self._dashboard["agents"]["knowledge_agent"] = "IDLE"
                    self.state_manager.set_agent_status("knowledge_agent", AgentStatus.IDLE)

                return

            # ── FAILED ───────────────────────────────────────────────────
            await self.event_bus.publish(Event(
                type=EventType.REMEDIATION_FAILED,
                source="self_healing_agent",
                incident_id=event.incident_id,
                data={
                    "status": result.status.value,
                    "errors": result.validation_errors,
                },
            ))

        except Exception as e:
            logger.error(f"[Orchestrator] Self-Healing Agent failed: {e}", exc_info=True)
            self.print_dashboard(f"Self-Healing Agent exception: {e}")
            await self.event_bus.publish(Event(
                type=EventType.REMEDIATION_FAILED,
                source="orchestrator",
                incident_id=event.incident_id,
                data={"status": RemediationStatus.FAILED.value, "errors": [str(e)]},
            ))
        finally:
            self._dashboard["agents"]["self_healing_agent"] = "IDLE"
            self.state_manager.set_agent_status("self_healing_agent", AgentStatus.IDLE)

    async def _on_remediation_complete(self, event: Event) -> None:
        files_fixed  = event.data.get("files_fixed", [])
        verification = event.data.get("verification", "not_run")
        self.state_manager.update_incident_status(event.incident_id, IncidentStatus.RESOLVED)
        self._track_incident(event.incident_id, "", "", "", "RESOLVED")
        self._dash("stage", "done")
        self.print_dashboard(f"RESOLVED — {len(files_fixed)} file(s) fixed, verification={verification}")
        await self._send_alert(
            incident_id = event.incident_id,
            title       = "✅ Incident Resolved",
            message     = f"Incident {event.incident_id} resolved. Files fixed: {files_fixed}.",
            severity    = event.data.get("severity", ""),
        )
        self.context_manager.drop_context(event.incident_id)

    async def _on_remediation_failed(self, event: Event) -> None:
        errors = event.data.get("errors", [])
        self.state_manager.update_incident_status(event.incident_id, IncidentStatus.FAILED)
        self._track_incident(event.incident_id, "", "", "", "FAILED")
        self._dash("stage", "done")
        self.print_dashboard(f"REMEDIATION FAILED — {errors[0] if errors else 'unknown'}")
        await self._send_alert(
            incident_id = event.incident_id,
            title       = "❌ Remediation Failed",
            message     = f"Incident {event.incident_id} could not be resolved. Errors: {errors}.",
            severity    = event.data.get("severity", "high"),
        )

    # ── Alerting (one-way notifications, not approval gates) ──────────────────

    async def _send_alert(
        self,
        incident_id: str,
        title:       str,
        message:     str,
        severity:    str = "",
    ) -> None:
        """
        Send a one-way alert notification via email.
        This is NOT an approval gate — no response is expected.

        Routing:
          Normal (low/medium or no severity) → EMAIL_TO only (primary dev).
          HIGH or CRITICAL                   → emergency lane: EMAIL_TO + full
                                               EMAIL_TEAM list simultaneously.

        Fires on:
          • Deployment complete (CI/CD status)
          • Incident resolved
          • Remediation failed
          • Manual action required
        """
        severity_upper = severity.upper()
        urgent = (
            severity_upper in ("HIGH", "CRITICAL")
            or any(
                kw in title.lower()
                for kw in ("failed", "critical", "manual action", "remediation failed")
            )
        )

        # Email alert (primary + optional team broadcast on urgent)
        if self._email:
            try:
                await self._email.send_alert(title=title, message=message, urgent=urgent)
            except Exception as exc:
                logger.error("[Orchestrator] Email alert failed: %s", exc)

        # Legacy alerting_agent path (no-op if not registered)
        alerting_agent = self.registry.get_agent("alerting_agent")
        if alerting_agent:
            try:
                await alerting_agent.send(incident_id=incident_id, title=title, message=message)
            except Exception as exc:
                logger.error("[Orchestrator] AlertingAgent failed: %s", exc)

    def summary(self) -> dict:
        return {
            "orchestrator" : "running" if self._running else "stopped",
            "agents"       : self.registry.summary(),
            "state"        : self.state_manager.summary(),
            "event_history": len(self.event_bus.get_history()),
        }

    def __repr__(self):
        return f"Orchestrator(running={self._running}, agents={self.registry.get_all_names()})"