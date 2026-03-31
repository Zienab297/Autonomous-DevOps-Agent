import sys
import os
import asyncio
import logging
from pathlib import Path
from datetime import datetime

# ── paths ──────────────────────────────────────────────────────────────────────
DEVOPS_AGENT_DIR = Path(__file__).resolve().parent
ROOT             = DEVOPS_AGENT_DIR.parent

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEVOPS_AGENT_DIR))

from controllers.agent_controller import AgentController
from agents.scaffold_agent.shared.config import load_config as load_scaffold_config
from agents.scaffold_agent.core_scaffold.scaffold_agent import ScaffoldAgent
from core.orchestrator import Orchestrator
from core.event_bus import EventType
from dotenv import load_dotenv

load_dotenv()
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")

# ── LLM Provider selector ──────────────────────────────────────────────────────
try:
    from providers.llm.llm_selector import (
        get_llm_provider,
        handle_quota_error,
        is_quota_error,
        get_all_agent_configs,
    )
    _LLM_SELECTOR_AVAILABLE = True
except ImportError:
    _LLM_SELECTOR_AVAILABLE = False

# ── Email client ───────────────────────────────────────────────────────────────
try:
    from core.email_client import EmailClient
    _EMAIL_MODULE_AVAILABLE = True
except ImportError:
    _EMAIL_MODULE_AVAILABLE = False

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
    # Import via fully-qualified package path only.
    # Never add knowledge_agent root to sys.path — both scaffold_agent and
    # knowledge_agent have a 'shared/' sub-package; adding the root causes
    # Python to resolve bare 'shared.models' to scaffold_agent/shared/models.py.
    try:
        from agents.knowledge_agent.knowledge_core.knowledge_agent_adapter import KnowledgeAgentAdapter
        return KnowledgeAgentAdapter
    except Exception as exc:
        return exc


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



# ── Email client builder ───────────────────────────────────────────────────────

def _build_email_client():
    """
    Build EmailClient from ALERT_* env vars.

    ALERT_ENGINEER_EMAIL  — primary developer; receives all approvals and alerts.
    ALERT_TEAM_EMAILS     — comma-separated; receives alerts when HIGH / CRITICAL.

    Returns None silently if required vars are missing.
    """
    if not _EMAIL_MODULE_AVAILABLE:
        return None

    host     = os.getenv("ALERT_SMTP_HOST",     "").strip()
    port_s   = os.getenv("ALERT_SMTP_PORT",     "587").strip()
    username = os.getenv("ALERT_SMTP_USERNAME", "").strip()
    password = os.getenv("ALERT_SMTP_PASSWORD", "").strip()
    from_    = os.getenv("ALERT_FROM_ADDRESS",  username).strip()
    engineer = os.getenv("ALERT_ENGINEER_EMAIL","").strip()

    team_raw = os.getenv("ALERT_TEAM_EMAILS", "").strip()
    team     = [a.strip() for a in team_raw.split(",") if a.strip()] if team_raw else []

    if not (host and username and password and engineer):
        return None

    try:
        client = EmailClient(
            smtp_host         = host,
            smtp_port         = int(port_s),
            username          = username,
            password          = password,
            from_address      = from_,
            engineer_email    = engineer,
            team_emails       = team,
            approval_base_url = os.getenv("ALERT_APPROVAL_BASE_URL", "").strip(),
        )
        team_info = f"  |  emergency team → {len(team)} address(es)" if team else ""
        print(f"  [Email] approvals/alerts → {engineer}{team_info}")
        return client
    except Exception as exc:
        print(f"  [Email] failed to init: {exc}")
        return None


# ── Dashboard ──────────────────────────────────────────────────────────────────
#
# FIX: Dashboard interference with agent output.
#
# ROOT CAUSE: The dashboard uses ANSI cursor-up sequences to redraw in-place.
# When an agent prints output while the dashboard is "running", the next
# redraw moves the cursor up N lines and overwrites the agent's output.
#
# SOLUTION:
#   1. pause() sets _first_draw=True so next draw starts FRESH (no cursor-up)
#   2. resume() does NOT immediately redraw — waits for the 5s loop tick
#   3. Loop interval is 5s (was 2s) — less interruption
#   4. Heavy agent output events (SCAFFOLD_STARTED, INCIDENT_CREATED, etc.)
#      automatically call pause() before their output and resume() after
#
# Result: agent output is always visible, dashboard only redraws when safe.

class Dashboard:

    def __init__(self, orchestrator: Orchestrator):
        self._orch        = orchestrator
        self._stage       = "INIT"
        self._project     = ""
        self._repo        = ""
        self._cicd_status = ""
        self._last_event  = ""
        self._start       = datetime.utcnow()
        self._paused      = False
        self._first_draw  = True
        self._last_lines  = 0
        self._task: asyncio.Task | None = None

    def start(self):
        self._task = asyncio.create_task(self._loop(), name="dashboard")

    def stop(self):
        if self._task and not self._task.done():
            self._task.cancel()
        sys.stdout.write("\n")
        sys.stdout.flush()

    def pause(self):
        """
        Stop dashboard redraws and reset to fresh-draw mode.
        After pause(), the next _draw() will print below current output
        instead of moving cursor up — so agent output is preserved.
        """
        self._paused     = True
        self._first_draw = True  # forces fresh draw on next resume

    def resume(self):
        """Resume redraws. Does NOT draw immediately — waits for loop tick."""
        self._paused = False

    def set_stage(self, stage: str):
        self._stage = stage

    def event(self, msg: str):
        self._last_event = msg

    async def _loop(self):
        while True:
            try:
                await asyncio.sleep(5)  # 5s interval — less interference
                if not self._paused:
                    self._draw()
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    def _draw(self):
        if self._paused:
            return

        now    = datetime.utcnow()
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
            sc3  = _RD if sev in ("critical", "high") else _YL
            st   = inc.status.value if hasattr(inc, "status") else "?"
            stc  = _YL if st != "resolved" else _GR
            desc = (inc.description[:30] + "…") if hasattr(inc, "description") and len(inc.description) > 30 else getattr(inc, "description", "")
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
            short = self._project if len(self._project) <= 48 else "…" + self._project[-47:]
            lines.append(f"  {_B}Project {_R} {_D}{short}{_R}")
        if self._repo:
            lines.append(f"  {_B}Repo    {_R} {_D}{self._repo}{_R}")
        if self._cicd_status:
            cc = _GR if "success" in self._cicd_status else _RD if "fail" in self._cicd_status else _YL
            lines.append(f"  {_B}CI/CD   {_R} {_B}{cc}{self._cicd_status.upper()}{_R}")

        lines.append(div)
        lines.append(f"  {_B}AGENTS{_R}")
        for a in ["scaffold_agent", "cicd_agent", "monitoring_agent",
                  "knowledge_agent", "self_healing_agent", "alerting_agent"]:
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
            # Fresh draw — just print, no cursor movement
            sys.stdout.write("\n".join(lines) + "\n")
            self._first_draw = False
            self._last_lines = len(lines)
        else:
            # In-place redraw — only safe when nothing else has printed
            n = self._last_lines
            sys.stdout.write(f"\033[{n+1}A\033[J" + "\n".join(lines) + "\n")
            self._last_lines = len(lines)

        sys.stdout.flush()


# ── Approval wrapper ───────────────────────────────────────────────────────────

def _patch_approval(approval_manager, dashboard: Dashboard):
    original = approval_manager.request_approval

    async def patched(title, details=None, context=None):
        dashboard.pause()
        await asyncio.sleep(0.1)
        try:
            return await original(title=title, details=details, context=context)
        finally:
            dashboard.resume()

    approval_manager.request_approval = patched


# ── LLM provider selection ────────────────────────────────────────────────────

def _select_llm_providers_upfront(dashboard: Dashboard) -> dict:
    if not _LLM_SELECTOR_AVAILABLE:
        return {}

    providers = {}
    agents = [
        ("scaffold",  "Scaffold Agent  — generates Dockerfile, k8s, CI/CD"),
        ("knowledge", "Knowledge Agent — RAG + LLM solution generation"),
        ("healing",   "Self-Healing Agent — applies code fixes"),
    ]

    print(f"\n{'═'*55}")
    print(f"  {_B}LLM Provider Setup{_R}")
    print(f"  Configure the AI model for each agent.")
    print(f"{'─'*55}")

    for agent_key, label in agents:
        try:
            provider = get_llm_provider(agent=agent_key)
            providers[agent_key] = provider
        except Exception as e:
            print(f"  {_YL}Skipped {agent_key}: {e}{_R}")

    print(f"{'═'*55}\n")
    return providers


def _attach_llm_providers_to_orchestrator(orchestrator, dashboard, providers):
    if not _LLM_SELECTOR_AVAILABLE or not providers:
        return

    _AGENT_KEY_MAP = {
        "scaffold_agent"    : "scaffold",
        "knowledge_agent"   : "knowledge",
        "self_healing_agent": "healing",
    }

    def _inject_provider(agent_name, agent_obj):
        selector_key = _AGENT_KEY_MAP.get(agent_name)
        if not selector_key:
            return
        provider = providers.get(selector_key)
        if not provider:
            return
        if hasattr(agent_obj, "set_llm_provider"):
            agent_obj.set_llm_provider(provider)
        if not hasattr(orchestrator, "llm_providers"):
            orchestrator.llm_providers = {}
        orchestrator.llm_providers[agent_name] = provider

    _orig_scaffold = orchestrator._on_scaffold_started
    async def _wrapped_scaffold(event):
        agent = orchestrator.registry.get_agent("scaffold_agent")
        if agent:
            _inject_provider("scaffold_agent", agent)
        await _orig_scaffold(event)
    orchestrator._on_scaffold_started = _wrapped_scaffold
    orchestrator.event_bus._subscribers.get("scaffold_started", []).clear()
    orchestrator.event_bus.subscribe(EventType.SCAFFOLD_STARTED, _wrapped_scaffold)

    _orig_incident = orchestrator._on_incident_created
    async def _wrapped_incident(event):
        agent = orchestrator.registry.get_agent("knowledge_agent")
        if agent:
            _inject_provider("knowledge_agent", agent)
        await _orig_incident(event)
    orchestrator._on_incident_created = _wrapped_incident
    orchestrator.event_bus._subscribers.get("incident_created", []).clear()
    orchestrator.event_bus.subscribe(EventType.INCIDENT_CREATED, _wrapped_incident)

    _orig_investigation = orchestrator._on_investigation_complete
    async def _wrapped_investigation(event):
        agent = orchestrator.registry.get_agent("self_healing_agent")
        if agent:
            _inject_provider("self_healing_agent", agent)
        await _orig_investigation(event)
    orchestrator._on_investigation_complete = _wrapped_investigation
    orchestrator.event_bus._subscribers.get("investigation_complete", []).clear()
    orchestrator.event_bus.subscribe(EventType.INVESTIGATION_COMPLETE, _wrapped_investigation)


# ── Event tracker — pauses dashboard around heavy output ──────────────────────

# Events that trigger heavy agent output → pause before, resume after
_PAUSE_BEFORE = {
    EventType.SCAFFOLD_STARTED,
    EventType.INCIDENT_CREATED,
    EventType.INVESTIGATION_COMPLETE,
}
_RESUME_AFTER = {
    EventType.SCAFFOLD_COMPLETE,
    EventType.SCAFFOLD_FAILED,
    EventType.DEPLOYMENT_COMPLETE,
    EventType.REMEDIATION_COMPLETE,
    EventType.REMEDIATION_FAILED,
}


# ── Main ───────────────────────────────────────────────────────────────────────

async def _run_scaffold():
    project_path    = str(Path.cwd())
    scaffold_config = load_scaffold_config()

    # ── Build email client from ALERT_* env vars ──────────────────────────
    print("\n  Checking notification channels...")
    email = _build_email_client()
    if not email:
        print("  [Channels] CLI only — add ALERT_SMTP_* to .env to enable email approvals/alerts")
    print()

    orchestrator = Orchestrator(email=email)
    dashboard    = Dashboard(orchestrator)

    _patch_approval(orchestrator.approval, dashboard)

    state_file = Path(project_path) / ".devops_state"
    first_run  = not state_file.exists()
    _print_logo()

    _SCAFFOLD_FILES = [
        "Dockerfile", "docker-compose.yml", ".dockerignore",
        ".github/workflows/deploy.yml",
        "k8s/deployment.yaml", "k8s/service.yaml", "k8s/ingress.yaml",
    ]
    _existing      = [f for f in _SCAFFOLD_FILES if (Path(project_path) / f).exists()]
    _skip_scaffold = False
    _run_flow      = True

    if not first_run:
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

        dashboard.pause()
        print(f"\n{'─'*55}")
        print(f"  Run the full DevOps pipeline again?")
        print(f"  (Scaffold → CI/CD → Monitor → Heal)")
        print(f"{'─'*55}")

        if _LLM_SELECTOR_AVAILABLE:
            saved = get_all_agent_configs()
            if saved:
                print(f"  {_D}Saved LLM providers:{_R}")
                for k, v in saved.items():
                    if isinstance(v, dict):
                        print(f"    {_D}{k:<12} → {v.get('provider','?').upper()} / {v.get('model','?')}{_R}")
                print()

        try:
            answer = input("  Proceed? [yes/no]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = "no"
        dashboard.resume()

        if answer not in ("yes", "y"):
            _run_flow = False
            print("  Skipping pipeline — launching chat agent.\n")

    # ── Select LLM providers upfront ─────────────────────────────────────
    llm_providers = _select_llm_providers_upfront(dashboard)
    _attach_llm_providers_to_orchestrator(orchestrator, dashboard, llm_providers)

    # ── Register agents ───────────────────────────────────────────────────
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
            orchestrator.register_agent("knowledge_agent", KnowledgeAgentAdapter())
            dashboard.event("knowledge agent registered")
        except Exception as e:
            dashboard.event(f"knowledge skipped — {e}")
    else:
        err = str(_ka_result) if isinstance(_ka_result, Exception) else "unavailable"
        dashboard.event(f"knowledge unavailable — {err}")

    await orchestrator.start()
    await orchestrator.start_approval_server()

    # ── Event tracker ──────────────────────────────────────────────────────
    orig_pub = orchestrator.event_bus.publish

    async def _tracked(event):
        # Pause dashboard BEFORE events that produce heavy output
        if event.type in _PAUSE_BEFORE:
            dashboard.pause()

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
                status = event.data.get("status", "")
                dashboard._cicd_status = (
                    "success" if status == "success"
                    else "failed" if status in ("failed", "failure")
                    else status
                )
                if event.data.get("repo_url"):
                    dashboard._repo = event.data["repo_url"]

        await orig_pub(event)

        # Resume dashboard AFTER events that are done producing output
        if event.type in _RESUME_AFTER:
            dashboard.resume()

    orchestrator.event_bus.publish = _tracked

    # ── Start dashboard ────────────────────────────────────────────────────
    dashboard.set_stage("SCAFFOLD")
    dashboard.start()
    dashboard._draw()

    if not _run_flow:
        dashboard.set_stage("DONE")
        dashboard.event("flow skipped — chat mode")
        dashboard._draw()
        await asyncio.sleep(1)
        dashboard.stop()
        await orchestrator.stop_approval_server()
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
    await orchestrator.stop_approval_server()
    # Cancel any lingering approval tasks so their input() calls don't
    # bleed into the post-pipeline menu prompt.
    for task in asyncio.all_tasks():
        if task.get_name().startswith("approval-"):
            task.cancel()
    await asyncio.sleep(0.1)  # let cancelled tasks finish cleanup


def main():
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    project_path = str(Path.cwd())
    state_file   = Path(project_path) / ".devops_state"

    try:
        asyncio.run(_run_scaffold())
    except KeyboardInterrupt:
        pass

    try:
        state_file.write_text(
            f"deployed_at={datetime.utcnow().isoformat()}\n"
            f"project={project_path}\n"
        )
    except Exception:
        pass

    print(f"\n{'─'*55}")
    print(f"  Pipeline complete. What would you like to do?")
    print(f"{'─'*55}")
    print(f"  [1] Run pipeline again")
    print(f"  [2] Open chat agent (manual tasks)")
    print(f"  [3] Exit")
    print(f"{'─'*55}")
    try:
        _choice = input("  Choose [1/2/3]: ").strip()
    except (EOFError, KeyboardInterrupt):
        _choice = "3"

    if _choice == "1":
        main()
        return
    elif _choice == "2":
        AgentController().run()


if __name__ == "__main__":
    main()