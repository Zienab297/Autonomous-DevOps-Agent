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
sys.path.insert(0, str(ROOT / "agents" / "scaffold_agent"))

from controllers.agent_controller import AgentController
from agents.scaffold_agent.shared.config import load_config as load_scaffold_config
from agents.scaffold_agent.core_scaffold.scaffold_agent import ScaffoldAgent
from core.orchestrator import Orchestrator
from core.event_bus import EventType
from dotenv import load_dotenv

load_dotenv()
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")

# ── Optional notification clients ──────────────────────────────────────────────
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
    """
    Load KnowledgeAgentAdapter without polluting sys.path with knowledge_agent/
    bare — which would cause `from shared.models import ProjectContext` inside
    ScaffoldAgent to resolve to knowledge_agent/shared instead of scaffold_agent/shared.
    """
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
_CL = "\033[2J\033[H" if _ANSI else ""

_LOGO = [
    "",
    f"  {_B}{_CY}██████╗ ███████╗██╗   ██╗ ██████╗ ██████╗███████╗{_R}",
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
    Build EmailClient from .env vars.

    Primary recipient  : EMAIL_TO           (required for email to be enabled)
    Team broadcast list: EMAIL_TEAM         (comma-separated, optional)
                         Used for the emergency lane when severity is HIGH/CRITICAL.

    Returns None silently if any required var is missing.
    """
    if not _EMAIL_MODULE_AVAILABLE:
        return None

    host     = os.getenv("SMTP_HOST",     "").strip()
    port_s   = os.getenv("SMTP_PORT",     "587").strip()
    username = os.getenv("SMTP_USERNAME", "").strip()
    password = os.getenv("SMTP_PASSWORD", "").strip()
    from_    = os.getenv("EMAIL_FROM",    username).strip()
    to_      = os.getenv("EMAIL_TO",      "").strip()

    # EMAIL_TEAM is a comma-separated list; strip whitespace around each address
    team_raw = os.getenv("EMAIL_TEAM", "").strip()
    team     = [a.strip() for a in team_raw.split(",") if a.strip()] if team_raw else []

    if not (host and username and password and to_):
        return None

    try:
        port   = int(port_s)
        client = EmailClient(
            smtp_host         = host,
            smtp_port         = port,
            username          = username,
            password          = password,
            from_address      = from_,
            to_address        = to_,
            team_addresses    = team,
            approval_base_url = os.getenv("APPROVAL_BASE_URL", "").strip(),
        )
        team_info = f"  |  team emergency lane → {len(team)} extra address(es)" if team else ""
        print(f"  [Email] ✓ alerts / approvals → {to_}{team_info}")
        return client
    except Exception as exc:
        print(f"  [Email] ✗ failed to init: {exc}")
        return None


# ── Live status dashboard ──────────────────────────────────────────────────────

class Dashboard:
    """Minimal in-terminal live status panel (redraws in-place on ANSI terminals)."""

    def __init__(self, orchestrator: Orchestrator):
        self._orch        = orchestrator
        self._stage       = "IDLE"
        self._last_event  = "starting…"
        self._project     = ""
        self._repo        = ""
        self._cicd_status = ""
        self._paused      = False
        self._task        = None
        self._first_draw  = True
        self._started_at  = datetime.utcnow()

    def set_stage(self, s: str):  self._stage = s
    def event(self, msg: str):    self._last_event = msg

    def pause(self):  self._paused = True
    def resume(self): self._paused = False

    def start(self):
        if _ANSI:
            self._task = asyncio.get_event_loop().call_later(2, self._tick)

    def stop(self):
        if self._task:
            self._task.cancel()

    def _tick(self):
        if not self._paused:
            self._draw()
        self._task = asyncio.get_event_loop().call_later(2, self._tick)

    def _draw(self):
        if self._paused:
            return

        now       = datetime.utcnow()
        elapsed   = int((now - self._started_at).total_seconds())
        uh, rem   = divmod(elapsed, 3600)
        um, us    = divmod(rem, 60)
        W         = 52

        incidents = self._orch.state_manager.get_active_incidents() if hasattr(
            self._orch.state_manager, "get_active_incidents") else []

        def agent_row(name):
            raw  = self._orch._dashboard["agents"].get(name, "IDLE")
            st   = str(raw).upper()
            stc  = _GR if st == "IDLE" else _YL if st in ("RUNNING","BUSY") else _RD
            return f"  {_D}{name:<22}{_R}  {_B}{stc}{st}{_R}"

        def inc_row(inc):
            sev  = getattr(inc, "severity", "?")
            svc  = getattr(inc, "service",  "?")
            desc = getattr(inc, "description", "")[:38]
            st   = str(getattr(inc, "status", "?")).upper()
            stc  = _GR if "RESOLV" in st else _RD if "FAIL" in st else _YL
            return (
                f"  {_D}{svc:<18}{_R}  {_B}{stc}{st}{_R}\n"
                f"           {_D}{desc}{_R}"
            )

        lines = []
        div   = f"  {_D}{'─'*W}{_R}"
        sc    = _GR if self._stage == "DONE" else _YL

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
            sys.stdout.write("\n".join(lines) + "\n")
            self._first_draw = False
        else:
            n = len(lines)
            up_seq = f"\033[{n + 1}A"
            sys.stdout.write(up_seq + "\033[J" + "\n".join(lines) + "\n")

        sys.stdout.flush()


# ── Approval wrapper ───────────────────────────────────────────────────────────

def _patch_approval(approval_manager, dashboard: Dashboard):
    """Pause dashboard during approval prompts so it never clears the input."""
    original = approval_manager.request_approval

    async def patched(title, details=None, context=None):
        dashboard.pause()
        await asyncio.sleep(0)
        try:
            return await original(title=title, details=details, context=context)
        finally:
            dashboard.resume()

    approval_manager.request_approval = patched


# ── Main ───────────────────────────────────────────────────────────────────────

async def _run_scaffold():
    project_path    = str(Path.cwd())
    scaffold_config = load_scaffold_config()

    # ── Build email client from .env ──────────────────────────────────────
    print("\n  Checking notification channels...")
    email = _build_email_client()
    if not email:
        print("  [Channels] CLI only — add SMTP_* vars to .env to enable email approvals and alerts")
    print()

    # ── Build orchestrator with email client ──────────────────────────────
    orchestrator = Orchestrator(email=email)
    dashboard    = Dashboard(orchestrator)

    _patch_approval(orchestrator.approval, dashboard)

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

    # ── Start approval server (email link callbacks) ───────────────────────
    await orchestrator.start_approval_server()

    # ── Event → dashboard tracker ──────────────────────────────────────────
    orig_pub = orchestrator.event_bus.publish

    async def _tracked(event):
        _stage_map = {
            EventType.SCAFFOLD_STARTED     : ("SCAFFOLD",    "scaffold agent running"),
            EventType.SCAFFOLD_COMPLETE    : ("CICD",        lambda e: f"scaffold done — {e.data.get('framework','?')} · {len(e.data.get('generated_files',[]))} files"),
            EventType.SCAFFOLD_FAILED      : ("DONE",        lambda e: f"scaffold failed — {e.data.get('error','')}"),
            EventType.DEPLOYMENT_COMPLETE  : ("MONITORING",  lambda e: f"ci/cd {e.data.get('status','?')} — {len(e.data.get('logs',[]))} log lines"),
            EventType.INCIDENT_CREATED     : ("INCIDENT",    lambda e: f"incident [{e.data.get('severity','?').upper()}] — {e.data.get('service','?')}"),
            EventType.INVESTIGATION_COMPLETE:("MONITORING",  "knowledge agent investigation complete"),
            EventType.REMEDIATION_COMPLETE : ("DONE",        "remediation complete — incident resolved"),
            EventType.REMEDIATION_FAILED   : ("DONE",        "remediation failed — manual intervention needed"),
        }
        if event.type in _stage_map:
            stage, msg = _stage_map[event.type]
            dashboard.set_stage(stage)
            dashboard.event(msg(event) if callable(msg) else msg)
            if event.type == EventType.DEPLOYMENT_COMPLETE:
                status = event.data.get("status", "")
                dashboard._cicd_status = (
                    "success" if status == "success"
                    else "failed" if status in ("failed","failure")
                    else status
                )
                if event.data.get("repo_url"):
                    dashboard._repo = event.data["repo_url"]
        await orig_pub(event)

    orchestrator.event_bus.publish = _tracked

    # ── Print logo once, then start live status panel ─────────────────────
    _print_logo()
    dashboard.set_stage("SCAFFOLD")
    dashboard.start()
    dashboard._draw()

    await orchestrator.run_scaffold(project_path=project_path)

    dashboard.set_stage("DONE")
    dashboard.event("pipeline complete")
    dashboard._draw()
    await asyncio.sleep(1)
    dashboard.stop()

    # ── Stop approval server after pipeline ends ───────────────────────────
    await orchestrator.stop_approval_server()


def main():
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(_run_scaffold())
    except KeyboardInterrupt:
        pass
    AgentController().run()


if __name__ == "__main__":
    main()