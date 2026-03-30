import sys
import os
import asyncio
import logging
from pathlib import Path
from datetime import datetime, timezone

# ── paths ──────────────────────────────────────────────────────────────────────
DEVOPS_AGENT_DIR = Path(__file__).resolve().parent
ROOT             = DEVOPS_AGENT_DIR.parent

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEVOPS_AGENT_DIR))
sys.path.insert(0, str(ROOT / "agents" / "scaffold_agent"))

from controllers.agent_controller import AgentController
from agents.scaffold_agent.shared.config import load_config as load_scaffold_config
from agents.scaffold_agent.core_scaffold.scaffold_agent import ScaffoldAgent
from core.orchestrator import Orchestrator
from db.session import dispose_engine
from core.event_bus import EventType
from dotenv import load_dotenv

load_dotenv()
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
DATABASE_URL  = os.getenv("DATABASE_URL", "")   # e.g. postgresql://user:pass@localhost/devops_db

# ── PostgreSQL database layer ──────────────────────────────────────────────────
try:
    from core.pg_database   import PostgreSQLDatabaseManager
    from core.interactive_cli import InteractiveCLI
    _PG_AVAILABLE = bool(DATABASE_URL)
except ImportError:
    _PG_AVAILABLE = False

# ── LLM Provider selector ──────────────────────────────────────────────────────
try:
    from providers.llm.llm_selector import (
        get_llm_provider,
        handle_quota_error,
        is_quota_error,
        get_all_agent_configs,
    )
    _LLM_SELECTOR_AVAILABLE = True
except ImportError as _llm_err:
    _LLM_SELECTOR_AVAILABLE = False

# ── Optional agents ────────────────────────────────────────────────────────────
try:
    from agents.cicd_agent.cicd_agent import CICDAgent
    from providers.cicd.github_provider import GitHubProvider
    _CICD_AVAILABLE = bool(GITHUB_TOKEN)
except ImportError:
    _CICD_AVAILABLE = False

try:
    from agents.monitoring_agent.agent import MonitoringAgent
    from agents.monitoring_agent.config import MonitoringConfig
    _MONITORING_AVAILABLE = True
except ImportError:
    _MONITORING_AVAILABLE = False

def _load_knowledge_adapter():
    import importlib.util
    ka_root      = ROOT / "agents" / "knowledge_agent"
    adapter_path = ka_root / "knowledge_core" / "knowledge_agent_adapter.py"
    if not adapter_path.exists():
        return FileNotFoundError(f"Not found: {adapter_path}")
    ka_root_s = str(ka_root)
    inserted  = ka_root_s not in sys.path
    saved     = {}
    try:
        if inserted:
            sys.path.insert(0, ka_root_s)
        for mod_name, rel in [
            ("shared",        "shared/__init__.py"),
            ("shared.models", "shared/models.py"),
            ("shared.config", "shared/config.py"),
        ]:
            fpath = ka_root / rel
            if not fpath.exists():
                continue
            if mod_name in sys.modules:
                saved[mod_name] = sys.modules[mod_name]
            spec = importlib.util.spec_from_file_location(mod_name, str(fpath))
            mod  = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = mod
            spec.loader.exec_module(mod)
        from agents.knowledge_agent.knowledge_core.knowledge_agent_adapter import KnowledgeAgentAdapter
        return KnowledgeAgentAdapter
    except Exception as exc:
        return exc
    finally:
        for k, v in saved.items():
            sys.modules[k] = v
        if inserted and ka_root_s in sys.path:
            sys.path.remove(ka_root_s)

_ka_result           = _load_knowledge_adapter()
_KNOWLEDGE_AVAILABLE = not isinstance(_ka_result, Exception)
if _KNOWLEDGE_AVAILABLE:
    KnowledgeAgentAdapter = _ka_result

# ── Silence noisy loggers ──────────────────────────────────────────────────────
logging.basicConfig(level=logging.WARNING, format="%(message)s")
for _lib in ["agents", "core", "httpx", "aiohttp", "urllib3", "groq",
             "qdrant_client", "sentence_transformers", "huggingface",
             "transformers", "filelock", "PIL"]:
    logging.getLogger(_lib).setLevel(logging.ERROR)

# ── ANSI ───────────────────────────────────────────────────────────────────────
import ctypes as _ctypes

def _ansi_supported() -> bool:
    if not sys.stdout.isatty():
        return False
    if sys.platform != "win32":
        return True
    try:
        kernel32 = _ctypes.windll.kernel32
        handle   = kernel32.GetStdHandle(-11)
        mode     = _ctypes.c_ulong(0)
        if not kernel32.GetConsoleMode(handle, _ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False

_ANSI = _ansi_supported()
_R  = "\033[0m"  if _ANSI else ""
_B  = "\033[1m"  if _ANSI else ""
_D  = "\033[2m"  if _ANSI else ""
_CY = "\033[36m" if _ANSI else ""
_GR = "\033[32m" if _ANSI else ""
_YL = "\033[33m" if _ANSI else ""
_RD = "\033[31m" if _ANSI else ""

_LOGO = [
    "",
    f"  {_B}{_CY}██████╗ ███████╗██╗   ██╗ ██████╗ ██████╗ ███████╗{_R}",
    f"  {_B}{_CY}██╔══██╗██╔════╝██║   ██║██╔═══██╗██╔══██╗██╔════╝{_R}",
    f"  {_B}{_CY}██║  ██║█████╗  ██║   ██║██║   ██║██████╔╝███████╗{_R}",
    f"  {_B}{_CY}██║  ██║██╔══╝  ╚██╗ ██╔╝██║   ██║██╔═══╝ ╚════██║{_R}",
    f"  {_B}{_CY}██████╔╝███████╗ ╚████╔╝ ╚██████╔╝██║     ███████║{_R}",
    f"  {_B}{_CY}╚═════╝ ╚══════╝  ╚═══╝   ╚═════╝ ╚═╝     ╚══════╝{_R}",
    f"  {_D}{'─'*52}{_R}",
    f"  {_D}Autonomous DevOps  ·  Scaffold · CI/CD · Monitor · Heal{_R}",
    "",
]

def _print_logo():
    for line in _LOGO:
        print(line)


# ── Dashboard ──────────────────────────────────────────────────────────────────

class Dashboard:
    _HEIGHT = 20

    def __init__(self, orchestrator: Orchestrator):
        self._orch        = orchestrator
        self._stage       = "INIT"
        self._project     = ""
        self._repo        = ""
        self._cicd_status = ""
        self._last_event  = ""
        self._start       = datetime.now(timezone.utc)
        self._paused      = False
        self._first_draw  = True
        self._task: asyncio.Task | None = None

    def start(self):
        self._task = asyncio.create_task(self._loop(), name="dashboard")

    def stop(self):
        if self._task and not self._task.done():
            self._task.cancel()
        sys.stdout.write("\n")
        sys.stdout.flush()

    def pause(self):
        self._paused = True
        sys.stdout.write("\n")
        sys.stdout.flush()

    def resume(self):
        self._paused     = False
        self._first_draw = True
        self._draw()

    def set_stage(self, stage: str):
        self._stage = stage

    def event(self, msg: str):
        self._last_event = msg

    async def _loop(self):
        while True:
            try:
                await asyncio.sleep(2)
                if not self._paused:
                    self._draw()
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    def _draw(self):
        now    = datetime.now(timezone.utc)
        up     = int((now - self._start).total_seconds())
        um, us = divmod(up, 60)
        uh, um = divmod(um, 60)
        W      = 60

        sc = {
            "SCAFFOLD" : _CY, "CICD"    : _CY,
            "MONITORING": _YL, "INCIDENT": _RD,
            "DONE"     : _GR,  "INIT"   : _D,
        }.get(self._stage, _CY)

        reg = self._orch.registry
        sm  = self._orch.state_manager

        def agent_row(name: str) -> str:
            rec    = reg.get(name)
            status = sm.get_agent_status(name) if rec else None
            if not rec or status is None:
                return f"  {_D}○ {name:<24}  —{_R}"
            sv     = status.value
            sc2    = _YL if sv == "running" else _GR if sv == "idle" else _RD
            bullet = "▶" if sv == "running" else "●"
            return f"  {_B}{bullet}{_R} {name:<24}  {_B}{sc2}{sv.upper()}{_R}"

        incidents = sm.get_active_incidents() if hasattr(sm, "get_active_incidents") else []

        def inc_row(inc) -> str:
            ts   = inc.created_at.strftime("%H:%M:%S") if hasattr(inc, "created_at") else ""
            sev  = inc.severity.value if hasattr(inc, "severity") else "?"
            sc3  = _RD if sev in ("critical","high") else _YL
            st   = inc.status.value  if hasattr(inc, "status") else "?"
            stc  = _YL if st != "resolved" else _GR
            desc = (inc.description[:30]+"…") if hasattr(inc,"description") and len(inc.description)>30 else getattr(inc,"description","")
            return (
                f"  {_D}{ts}{_R}  {_B}{sc3}{sev.upper():<8}{_R}"
                f"  {_D}{inc.service:<18}{_R}  {_B}{stc}{st}{_R}\n"
                f"           {_D}{desc}{_R}"
            )

        lines = []
        div   = f"  {_D}{'─'*W}{_R}"

        lines.append(div)
        lines.append(
            f"  {_B}Stage   {_R} {_B}{sc}{self._stage:<10}{_R}"
            f"  {_D}uptime {uh:02d}:{um:02d}:{us:02d}{_R}"
        )
        if self._project:
            short = self._project if len(self._project)<=48 else "…"+self._project[-47:]
            lines.append(f"  {_B}Project {_R} {_D}{short}{_R}")
        if self._repo:
            lines.append(f"  {_B}Repo    {_R} {_D}{self._repo}{_R}")
        if self._cicd_status:
            cc = _GR if "success" in self._cicd_status else _RD if "fail" in self._cicd_status else _YL
            lines.append(f"  {_B}CI/CD   {_R} {_B}{cc}{self._cicd_status.upper()}{_R}")

        lines.append(div)
        lines.append(f"  {_B}AGENTS{_R}")
        for a in ["scaffold_agent","cicd_agent","monitoring_agent",
                  "knowledge_agent","self_healing_agent","alerting_agent"]:
            lines.append(agent_row(a))

        lines.append(div)
        lines.append(f"  {_B}INCIDENTS{_R}  {_D}({len(incidents)} active){_R}")
        if incidents:
            for inc in incidents:
                lines.append(inc_row(inc))
        else:
            lines.append(f"  {_D}none{_R}")

        lines.append(div)
        lines.append(f"  {_D}{self._last_event}{_R}")
        lines.append(div)

        if self._first_draw or not _ANSI:
            sys.stdout.write("\n".join(lines)+"\n")
            self._first_draw = False
        else:
            n = len(lines)
            sys.stdout.write(f"\033[{n+1}A\033[J"+"\n".join(lines)+"\n")
        sys.stdout.flush()


# ── Approval wrapper ───────────────────────────────────────────────────────────

def _patch_approval(approval_manager, dashboard: Dashboard):
    original = approval_manager.request_approval

    async def patched(title, details=None, context=None):
        dashboard.pause()
        await asyncio.sleep(0)
        try:
            return await original(title=title, details=details, context=context)
        finally:
            dashboard.resume()

    approval_manager.request_approval = patched


# ── LLM provider injection into orchestrator ──────────────────────────────────

def _attach_llm_providers_to_orchestrator(orchestrator: Orchestrator, dashboard: Dashboard):
    """
    Wrap the orchestrator's agent-calling methods so that:
    1. Just before each agent runs → get_llm_provider(agent=...) is called
       (uses saved config or asks interactively)
    2. If quota error → handle_quota_error() asks for new provider
    3. Provider is passed to the agent via set_llm_provider() if supported

    This way the user is asked per-agent at the moment it's needed,
    not all upfront.
    """
    if not _LLM_SELECTOR_AVAILABLE:
        return

    # Map: agent registry name → llm_selector agent key
    _AGENT_KEY_MAP = {
        "scaffold_agent"   : "scaffold",
        "knowledge_agent"  : "knowledge",
        "self_healing_agent": "healing",
    }

    def _inject_provider(agent_name: str, agent_obj):
        """Get provider for agent and inject via set_llm_provider if supported."""
        selector_key = _AGENT_KEY_MAP.get(agent_name)
        if not selector_key:
            return
        try:
            dashboard.pause()
            provider = get_llm_provider(agent=selector_key)
            dashboard.resume()
            if hasattr(agent_obj, "set_llm_provider"):
                agent_obj.set_llm_provider(provider)
            # Store on orchestrator too for reference
            if not hasattr(orchestrator, "llm_providers"):
                orchestrator.llm_providers = {}
            orchestrator.llm_providers[agent_name] = provider
        except Exception as e:
            dashboard.resume()
            dashboard.event(f"LLM provider error for {agent_name}: {e}")

    # Wrap orchestrator._on_scaffold_started to inject before scaffold runs
    _orig_scaffold = orchestrator._on_scaffold_started

    async def _wrapped_scaffold(event):
        agent = orchestrator.registry.get_agent("scaffold_agent")
        if agent:
            _inject_provider("scaffold_agent", agent)
        await _orig_scaffold(event)

    orchestrator._on_scaffold_started = _wrapped_scaffold
    # Re-subscribe with wrapped version
    orchestrator.event_bus._subscribers.get("scaffold_started", []).clear()
    orchestrator.event_bus.subscribe(EventType.SCAFFOLD_STARTED, _wrapped_scaffold)

    # Wrap _on_incident_created to inject before knowledge agent runs
    _orig_incident = orchestrator._on_incident_created

    async def _wrapped_incident(event):
        agent = orchestrator.registry.get_agent("knowledge_agent")
        if agent:
            _inject_provider("knowledge_agent", agent)
        await _orig_incident(event)

    orchestrator._on_incident_created = _wrapped_incident
    orchestrator.event_bus._subscribers.get("incident_created", []).clear()
    orchestrator.event_bus.subscribe(EventType.INCIDENT_CREATED, _wrapped_incident)

    # Wrap _on_investigation_complete to inject before self-healing runs
    _orig_investigation = orchestrator._on_investigation_complete

    async def _wrapped_investigation(event):
        agent = orchestrator.registry.get_agent("self_healing_agent")
        if agent:
            _inject_provider("self_healing_agent", agent)
        await _orig_investigation(event)

    orchestrator._on_investigation_complete = _wrapped_investigation
    orchestrator.event_bus._subscribers.get("investigation_complete", []).clear()
    orchestrator.event_bus.subscribe(EventType.INVESTIGATION_COMPLETE, _wrapped_investigation)


# ── Main ───────────────────────────────────────────────────────────────────────

async def _run_scaffold():
    project_path    = str(Path.cwd())
    scaffold_config = load_scaffold_config()
    orchestrator    = Orchestrator()
    dashboard       = Dashboard(orchestrator)

    _patch_approval(orchestrator.approval, dashboard)

    # ── Has this project been deployed before? ─────────────────────────────
    state_file = Path(project_path) / ".devops_state"
    first_run  = not state_file.exists()
    _print_logo()  # always show logo

    _SCAFFOLD_FILES = [
        "Dockerfile", "docker-compose.yml", ".dockerignore",
        ".github/workflows/deploy.yml",
        "k8s/deployment.yaml", "k8s/service.yaml", "k8s/ingress.yaml",
    ]
    _existing      = [f for f in _SCAFFOLD_FILES if (Path(project_path) / f).exists()]
    _skip_scaffold = False
    _run_flow      = True

    if not first_run:
        # ── Ask: re-generate scaffold? ────────────────────────────────────
        if _existing:
            dashboard.pause()
            print(f"\n{'─'*55}")
            print(f"  This project was previously deployed.")
            print(f"  Scaffold files already exist ({len(_existing)} files).")
            print(f"{'─'*55}")
            try:
                answer = input("  Re-generate scaffold files? [yes/no]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                answer = "no"
            dashboard.resume()
            if answer not in ("yes", "y"):
                _skip_scaffold = True

        # ── Ask: run pipeline again? ──────────────────────────────────────
        dashboard.pause()
        print(f"\n{'─'*55}")
        print(f"  Run the full DevOps pipeline again?")
        print(f"  (Scaffold → CI/CD → Monitor → Heal)")
        print(f"{'─'*55}")

        # Show saved LLM configs if available
        if _LLM_SELECTOR_AVAILABLE:
            saved = get_all_agent_configs()
            if saved:
                print(f"  {_D}Saved LLM providers:{_R}")
                for k, v in saved.items():
                    if isinstance(v, dict):
                        _prov = v.get('provider', '?').upper()
                        _mod  = v.get('model', '?')
                        print(f"    {_D}{k:<12} → {_prov} / {_mod}{_R}")
                print()

        try:
            answer = input("  Proceed? [yes/no]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = "no"
        dashboard.resume()

        if answer not in ("yes", "y"):
            _run_flow = False
            print("  Skipping pipeline — launching chat agent.\n")

    # ── Attach per-agent LLM provider injection ────────────────────────────
    # This wraps the orchestrator methods so each agent is asked
    # for its LLM provider at the moment it's about to run.
    _attach_llm_providers_to_orchestrator(orchestrator, dashboard)

    # ── Register agents ────────────────────────────────────────────────────
    orchestrator.register_agent("scaffold_agent", ScaffoldAgent(scaffold_config))
    dashboard._project = project_path
    dashboard.event("scaffold ready")

    if _CICD_AVAILABLE:
        provider   = GitHubProvider(token=GITHUB_TOKEN, org="")
        cicd_agent = CICDAgent(
            provider    = provider,
            event_bus   = orchestrator.event_bus,
            registry    = orchestrator.registry,
            state       = orchestrator.state_manager,
            ctx_manager = orchestrator.context_manager,
        )
        await cicd_agent.start()
        orchestrator.register_agent("cicd_agent", cicd_agent)
        dashboard.event("scaffold + cicd ready")
    else:
        dashboard.event("cicd skipped — no GITHUB_TOKEN")

    if _MONITORING_AVAILABLE:
        monitoring_agent = MonitoringAgent(
            event_bus       = orchestrator.event_bus,
            registry        = orchestrator.registry,
            config          = MonitoringConfig(
                collector_backend = "file",
                log_dir           = "logs",
                poll_interval     = 30.0,
            ),
            context_manager = orchestrator.context_manager,
            state_manager   = orchestrator.state_manager,
            groq_api_key    = os.getenv("GROQ_API_KEY"),
            live_dashboard  = False,
        )
        await monitoring_agent.start()
        orchestrator.register_agent("monitoring_agent", monitoring_agent)
        dashboard.event("monitoring started")

    if _KNOWLEDGE_AVAILABLE:
        try:
            ka_root_s = str(ROOT / "agents" / "knowledge_agent")
            _added    = ka_root_s not in sys.path
            if _added:
                sys.path.insert(0, ka_root_s)
            try:
                orchestrator.register_agent("knowledge_agent", KnowledgeAgentAdapter())
                dashboard.event("knowledge agent registered")
            finally:
                if _added and ka_root_s in sys.path:
                    sys.path.remove(ka_root_s)
        except Exception as e:
            dashboard.event(f"knowledge skipped — {e}")
    else:
        err = str(_ka_result) if isinstance(_ka_result, Exception) else "unavailable"
        dashboard.event(f"knowledge unavailable — {err}")

    await orchestrator.start()

    # ── Wire PostgreSQL DB (if configured) ────────────────────────────────
    if _PG_AVAILABLE:
        try:
            pg_db = PostgreSQLDatabaseManager.for_project(
                project_path = project_path,
                database_url = DATABASE_URL,
            )
            orchestrator.set_pg_database(pg_db)
            dashboard.event("PostgreSQL connected")
        except Exception as _pg_err:
            dashboard.event(f"PostgreSQL skipped — {_pg_err}")

    # ── event → dashboard tracker ──────────────────────────────────────────
    orig_pub = orchestrator.event_bus.publish

    async def _tracked(event):
        _stage_map = {
            EventType.SCAFFOLD_STARTED     : ("SCAFFOLD",   "scaffold agent running"),
            EventType.SCAFFOLD_COMPLETE    : ("CICD",       lambda e: f"scaffold done — {e.data.get('framework','?')} · {len(e.data.get('generated_files',[]))} files"),
            EventType.SCAFFOLD_FAILED      : ("DONE",       lambda e: f"scaffold failed — {e.data.get('error','')}"),
            EventType.DEPLOYMENT_COMPLETE  : ("MONITORING", lambda e: f"ci/cd {e.data.get('status','?')} — {len(e.data.get('logs',[]))} log lines"),
            EventType.INCIDENT_CREATED     : ("INCIDENT",   lambda e: f"incident [{e.data.get('severity','?').upper()}] — {e.data.get('service','?')}"),
            EventType.INVESTIGATION_COMPLETE:("MONITORING", "knowledge agent investigation complete"),
            EventType.REMEDIATION_COMPLETE : ("DONE",       "remediation complete — incident resolved"),
            EventType.REMEDIATION_FAILED   : ("DONE",       "remediation failed — manual intervention needed"),
        }
        if event.type in _stage_map:
            stage, msg = _stage_map[event.type]
            dashboard.set_stage(stage)
            dashboard.event(msg(event) if callable(msg) else msg)
            if event.type == EventType.DEPLOYMENT_COMPLETE:
                status = event.data.get("status","")
                dashboard._cicd_status = (
                    "success" if status=="success"
                    else "failed" if status in ("failed","failure")
                    else status
                )
                if event.data.get("repo_url"):
                    dashboard._repo = event.data["repo_url"]
        await orig_pub(event)

    orchestrator.event_bus.publish = _tracked

    # ── start dashboard ──────────────────────────────────────────────────
    dashboard.set_stage("SCAFFOLD")
    dashboard.start()
    dashboard._draw()

    if not _run_flow:
        dashboard.set_stage("DONE")
        dashboard.event("flow skipped — chat mode")
        dashboard._draw()
        await asyncio.sleep(1)
        dashboard.stop()
        return

    await orchestrator.run_scaffold(
        project_path  = project_path,
        dry_run       = False,
        skip_scaffold = _skip_scaffold,
    )

    dashboard.set_stage("DONE")
    dashboard.event("pipeline complete")
    dashboard._draw()
    await asyncio.sleep(1)
    dashboard.stop()


def _show_main_menu() -> str:
    print(f"\n{'═'*55}")
    print(f"  {_B}{_CY}Autonomous DevOps Agent{_R}")
    print(f"{'─'*55}")
    print(f"  {_B}[1]{_R}  Run pipeline  {_D}(Scaffold → CI/CD → Monitor → Heal){_R}")
    print(f"  {_B}[2]{_R}  Open chat agent  {_D}(manual tasks){_R}")
    print(f"  {_B}[3]{_R}  Query history  {_D}(incidents, events, solutions){_R}")
    print(f"  {_B}[4]{_R}  Exit")
    print(f"{'─'*55}")
    try:
        return input("  Choose [1/2/3/4]: ").strip()
    except (EOFError, KeyboardInterrupt):
        return "4"


def _open_interactive_cli(project_path: str) -> None:
    """Launch the interactive history query CLI."""
    # Try PostgreSQL first, fall back to SQLite
    db   = None
    pid  = ""

    if _PG_AVAILABLE:
        try:
            db  = PostgreSQLDatabaseManager.for_project(project_path, DATABASE_URL)
            pid = db.project_id
        except Exception as e:
            print(f"  {_YL}PostgreSQL unavailable: {e}{_R}")

    if db is None:
        # Try SQLite fallback
        try:
            from core.database import DatabaseManager
            db  = DatabaseManager.for_project(project_path)
            pid = db.project
        except Exception:
            print(f"  {_RD}No database configured. Set DATABASE_URL in .env for PostgreSQL.{_R}")
            return

    try:
        from core.interactive_cli import InteractiveCLI
        cli = InteractiveCLI(db=db, project_id=pid)
        cli.run()
    except ImportError:
        print(f"  {_RD}Interactive CLI not available.{_R}")


def main():
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    project_path = str(Path.cwd())
    state_file   = Path(project_path) / ".devops_state"

    _print_logo()

    while True:
        choice = _show_main_menu()

        if choice == "1":
            try:
                asyncio.run(_run_scaffold())
            except KeyboardInterrupt:
                pass

            # Save state after first successful run
            try:
                state_file.write_text(
                    f"deployed_at={datetime.now(timezone.utc).isoformat()}\n"
                    f"project={project_path}\n"
                )
            except Exception:
                pass

        elif choice == "2":
            AgentController().run()

        elif choice == "3":
            _open_interactive_cli(project_path)

        elif choice == "4":
            print(f"\n  {_D}Goodbye.{_R}\n")
            try:
                dispose_engine()
            except Exception:
                pass
            break


if __name__ == "__main__":
    main()