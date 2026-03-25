"""
The MonitoringAgent polls services on a fixed interval,
detects anomalies, and publishes INCIDENT_CREATED events
to the EventBus when something goes wrong.

Lifecycle
---------
    agent = MonitoringAgent(bus, registry, config)
    await agent.start()   # registers, starts poll loop + live dashboard
    ...
    await agent.stop()    # cancels loop, unregisters

Event flow
----------
    [poll loop]
        collector.collect_metrics(service) → List[Metric]
        collector.collect_logs(service)    → List[Log]
        detector.analyze(...)              → List[Anomaly]
        incident_factory.create(...)       → Incident
        groq_analyzer.analyze(...)         → IncidentAnalysis  <- LLM step
            patches Incident.severity, description, metadata
        EventBus.publish(INCIDENT_CREATED)

    INCIDENT_CREATED event data includes:
        "files_to_fix" : [{"file": "deploy.py", "line": 47, ...}, ...]
        <- populated from CI/CD log tracebacks when collector_backend="file"

Live Dashboard
--------------
    Redraws the terminal every 2 seconds showing:
        - per-service health + last metric values (colour-coded)
        - active incidents with severity and age
        - rolling event log (last 8 entries)
    Runs only when stdout is a real TTY.

One-shot log analysis (called by Orchestrator after CI/CD)
----------------------------------------------------------
    incident = await agent.analyze_logs(raw_log_lines)
"""

import asyncio
import logging
import sys
from datetime import datetime
from typing import List, Optional

from core.base_agent import BaseAgent, AgentEvent
from core.event_bus import EventBus, Event, EventType
from core.agent_registery import AgentRegistry
from core.context_manager import ContextManager
from core.models import Incident, Log

from agents.monitoring_agent.config import MonitoringConfig
from agents.monitoring_agent.collector import BaseCollector, MockCollector
from agents.monitoring_agent.detector import Detector
from agents.monitoring_agent.incident_factory import IncidentFactory
from agents.monitoring_agent.groq_analyzer import GroqAnalyzer

logger = logging.getLogger(__name__)

# ── ANSI colour helpers ───────────────────────────────────────────────────────
_RESET  = "\033[0m"
_BOLD   = "\033[1m"
_DIM    = "\033[2m"
_RED    = "\033[31m"
_YELLOW = "\033[33m"
_GREEN  = "\033[32m"
_CYAN   = "\033[36m"
_WHITE  = "\033[97m"
_CLEAR  = "\033[2J\033[H"   # clear screen + home cursor

_SEV_COLOR = {"critical": _RED, "high": _RED, "medium": _YELLOW, "low": _GREEN}

def _sev(s: str) -> str:
    c = _SEV_COLOR.get(s.lower(), _WHITE)
    return f"{_BOLD}{c}{s.upper():<8}{_RESET}"

def _age(dt: datetime) -> str:
    secs = int((datetime.utcnow() - dt).total_seconds())
    if secs < 60:   return f"{secs}s ago"
    if secs < 3600: return f"{secs // 60}m ago"
    return f"{secs // 3600}h ago"


class MonitoringAgent(BaseAgent):
    """
    Polls all configured services and fires INCIDENT_CREATED events
    when anomalies are detected.

    Also runs a live terminal dashboard (redraws every 2s) showing
    per-service health, active incidents, and a scrolling event log.

    File backend example (CI/CD log monitoring):
        config = MonitoringConfig(
            services=["auth-api", "payments-api"],
            poll_interval=30.0,
            collector_backend="file",
            log_dir="logs",
        )
        agent = MonitoringAgent(event_bus=bus, registry=registry, config=config)
        await agent.start()

    Mock backend example (development):
        config = MonitoringConfig(services=["auth-api"], collector_backend="mock")
    """

    def __init__(
        self,
        event_bus       : EventBus,
        registry        : AgentRegistry,
        config          : Optional[MonitoringConfig] = None,
        collector       : Optional[BaseCollector] = None,
        context_manager : Optional[ContextManager] = None,
        state_manager   = None,
        groq_api_key    : Optional[str] = None,
        live_dashboard  : bool = True,
    ):
        super().__init__(
            name       = "monitoring_agent",
            event_bus  = event_bus,
            registry   = registry,
        )
        self._config          = config or MonitoringConfig()
        self._context_manager = context_manager
        self._state_manager   = state_manager
        self._live_dashboard  = live_dashboard

        # Collector: use the injected one, or build from config
        self._collector = collector or self._build_collector()

        # Stateless helpers — created once, reused every poll
        self._detector = Detector(self._config.thresholds)
        self._factory  = IncidentFactory()

        # Groq LLM analyzer — enriches incidents before they are published
        self._analyzer = GroqAnalyzer(api_key=groq_api_key)

        # Background tasks
        self._poll_task      : Optional[asyncio.Task] = None
        self._dashboard_task : Optional[asyncio.Task] = None
        self._started        : bool = False

        # Track which incidents we've already created this session
        # (service -> incident_id) to avoid duplicate incidents for the same ongoing anomaly
        self._active_incidents: dict[str, str] = {}

        # ── Dashboard state (updated live by _poll_service) ───────────────
        self._service_state: dict[str, dict] = {
            s: {"status": "pending", "metrics": {}, "last_poll": None, "anomaly_count": 0}
            for s in self._config.services
        }
        self._event_log  : list[tuple[datetime, str]] = []  # rolling terminal log
        self._poll_count : int = 0
        self._agent_start: datetime = datetime.utcnow()

    # --------------------------------------------------------
    # BaseAgent lifecycle hooks
    # --------------------------------------------------------

    async def _setup(self) -> None:
        """Start the background polling loop, dashboard, and event subscription."""
        if self._started:
            self.logger.debug("[MonitoringAgent] _setup called again — already running, skipping")
            return
        self._started = True
        # Subscribe so the agent reacts automatically when CI/CD finishes
        self.subscribe(EventType.DEPLOYMENT_COMPLETE, self._on_deployment_complete)

        self._poll_task = asyncio.create_task(
            self._poll_loop(),
            name="monitoring_agent_poll_loop",
        )

        if self._live_dashboard and sys.stdout.isatty():
            self._dashboard_task = asyncio.create_task(
                self._dashboard_loop(),
                name="monitoring_agent_dashboard",
            )

        self.logger.info(
            "[MonitoringAgent] Started (interval=%.1fs, services=%s, backend=%s, groq=%s, dashboard=%s)",
            self._config.poll_interval,
            self._config.services,
            self._config.collector_backend,
            "enabled" if self._analyzer.available else "fallback",
            "on" if self._dashboard_task else "off",
        )

    async def _teardown(self) -> None:
        """Cancel the polling loop and dashboard gracefully."""
        self._started = False
        for task in (self._poll_task, self._dashboard_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        if self._live_dashboard and sys.stdout.isatty():
            print(_RESET)
        self.logger.info("[MonitoringAgent] Stopped")

    async def handle_event(self, event: AgentEvent) -> None:
        """
        The MonitoringAgent is primarily a producer.
        DEPLOYMENT_COMPLETE is handled via _on_deployment_complete.
        """

    # --------------------------------------------------------
    # CI/CD integration — triggered by DEPLOYMENT_COMPLETE event
    # --------------------------------------------------------

    async def _on_deployment_complete(self, event: Event) -> None:
        """React automatically when the CI/CD agent finishes a run."""
        logs_raw: List[str] = event.data.get("logs", [])
        if not logs_raw:
            return
        project_path = event.data.get("project_path", "cicd-pipeline")
        self._log_event(f"DEPLOYMENT_COMPLETE — {len(logs_raw)} lines from {project_path}")
        log_objects = self._strings_to_logs(logs_raw, service=project_path)
        incident = await self._run_analysis_pipeline(service=project_path, logs=log_objects)
        if incident:
            await self.publish(Event(
                type        = EventType.INCIDENT_CREATED,
                source      = self.name,
                incident_id = incident.incident_id,
                data        = self._incident_payload(incident),
            ))

    # --------------------------------------------------------
    # One-shot public method — called directly by the Orchestrator
    # --------------------------------------------------------

    async def analyze_logs(self, logs: List[str]) -> Optional[Incident]:
        """
        One-shot analysis of raw CI/CD log lines.
        Called by the Orchestrator immediately after CI/CD completes:

            incident = await monitoring_agent.analyze_logs(logs)

        Returns an Incident if anomalies are found, else None.
        Does NOT publish any events — the Orchestrator calls handle_incident().
        """
        if not logs:
            return None
        service = "cicd-pipeline"
        self._log_event(f"analyze_logs(): {len(logs)} lines for '{service}'")
        log_objects = self._strings_to_logs(logs, service=service)
        return await self._run_analysis_pipeline(service=service, logs=log_objects)

    # --------------------------------------------------------
    # Shared analysis pipeline (poll loop + one-shot path)
    # --------------------------------------------------------

    async def _run_analysis_pipeline(
        self,
        service : str,
        logs    : list,
        metrics : Optional[list] = None,
    ) -> Optional[Incident]:
        """
        Detect -> IncidentFactory -> GroqAnalyzer.
        Shared by _poll_service() and analyze_logs().
        Returns enriched Incident or None. Does NOT publish events.
        """
        metrics = metrics or []
        anomalies = self._detector.analyze(service, metrics, logs)
        if not anomalies:
            return None

        incident = self._factory.create(
            service   = service,
            anomalies = anomalies,
            metrics   = metrics,
            logs      = logs,
        )
        analysis = await self._analyzer.analyze(
            service   = service,
            anomalies = anomalies,
            metrics   = metrics,
            logs      = logs,
        )
        incident.severity    = analysis.severity
        incident.description = analysis.root_cause
        incident.metadata["llm_analysis"] = {
            "model"       : analysis.model,
            "severity"    : analysis.severity.value,
            "root_cause"  : analysis.root_cause,
            "impact"      : analysis.impact,
            "recommended" : analysis.recommended,
            "confidence"  : analysis.confidence,
            "report"      : analysis.report,
            "files_to_fix": analysis.files_to_fix,
            "fallback"    : analysis.fallback,
        }
        self.logger.info(
            "[MonitoringAgent] Groq: severity=%s confidence=%.0f%% files_to_fix=%d fallback=%s",
            analysis.severity.value, analysis.confidence * 100,
            len(analysis.files_to_fix), analysis.fallback,
        )
        return incident

    @staticmethod
    def _strings_to_logs(raw_lines: List[str], service: str) -> list:
        """Convert raw CI/CD log strings to Log objects the Detector can process."""
        logs = []
        for line in raw_lines:
            upper = line.upper()
            if any(k in upper for k in ("ERROR", "FAIL", "TRACEBACK", "EXCEPTION", "CRITICAL")):
                level = "ERROR"
            elif any(k in upper for k in ("WARNING", "WARN")):
                level = "WARN"
            else:
                level = "INFO"
            logs.append(Log(
                message   = line,
                level     = level,
                service   = service,
                timestamp = datetime.utcnow(),
                metadata  = {},
            ))
        return logs

    @staticmethod
    def _incident_payload(incident: Incident) -> dict:
        """Serialize Incident to the standard INCIDENT_CREATED event payload."""
        llm = incident.metadata.get("llm_analysis", {})
        return {
            "incident_id"  : incident.incident_id,
            "service"      : incident.service,
            "severity"     : incident.severity.value,
            "description"  : incident.description,
            "impact"       : llm.get("impact", ""),
            "recommended"  : llm.get("recommended", ""),
            "confidence"   : llm.get("confidence", 0.0),
            "report"       : llm.get("report", ""),
            "anomaly_count": incident.metadata.get("anomaly_count", 0),
            "llm_fallback" : llm.get("fallback", True),
            "files_to_fix" : llm.get("files_to_fix", []),
        }

    def _log_event(self, msg: str) -> None:
        """Append a timestamped entry to the rolling dashboard event log."""
        self._event_log.append((datetime.utcnow(), msg))
        if len(self._event_log) > 50:
            self._event_log.pop(0)

    # --------------------------------------------------------
    # Polling loop
    # --------------------------------------------------------

    async def _poll_loop(self) -> None:
        """
        Main loop: poll every service, detect anomalies, publish incidents.
        Sleep-first design: waits one full interval before the first poll.
        """
        self.logger.info(
            "[MonitoringAgent] Poll loop running — first poll in %.0fs",
            self._config.poll_interval,
        )
        while True:
            try:
                await asyncio.sleep(self._config.poll_interval)
            except asyncio.CancelledError:
                break
            try:
                await self._poll_all_services()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.logger.error(
                    "[MonitoringAgent] Unexpected error in poll loop: %s",
                    e, exc_info=True,
                )

    async def _poll_all_services(self) -> None:
        """Poll all configured services concurrently."""
        tasks = [self._poll_service(s) for s in self._config.services]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _poll_service(self, service: str) -> None:
        """
        Full poll cycle for a single service:
            collect -> detect -> maybe create incident -> maybe publish event.
        Updates live dashboard state on every call.
        """
        try:
            # 1. Collect
            metrics = await self._collector.collect_metrics(service)
            logs    = await self._collector.collect_logs(
                service, max_lines=self._config.max_log_lines
            )

            self._poll_count += 1
            self._service_state[service]["last_poll"] = datetime.utcnow()
            self._service_state[service]["metrics"]   = {m.name: m.value for m in metrics}

            # 2. Detect anomalies
            anomalies = self._detector.analyze(service, metrics, logs)

            if not anomalies:
                if service in self._active_incidents:
                    self._log_event(f"OK  {service} — returned to healthy")
                    del self._active_incidents[service]
                self._service_state[service]["status"]        = "healthy"
                self._service_state[service]["anomaly_count"] = 0
                return

            # 3. Avoid flooding — one active incident per service at a time
            if service in self._active_incidents:
                return

            # 4+5. Build + enrich via shared pipeline
            incident = await self._run_analysis_pipeline(
                service = service,
                logs    = logs,
                metrics = metrics,
            )
            if not incident:
                return

            # 6. Track + update dashboard
            self._active_incidents[service] = incident.incident_id
            self._service_state[service]["status"]        = "incident"
            self._service_state[service]["anomaly_count"] = len(anomalies)
            self._log_event(
                f"!!! INCIDENT {incident.incident_id} "
                f"[{incident.severity.value.upper()}] — {service}"
            )

            # 7. Store in StateManager / ContextManager
            if self._state_manager:
                self._state_manager.add_incident(incident)
            if self._context_manager:
                self._context_manager.create_context(incident)
                self._context_manager.add_metrics(incident.incident_id, metrics)
                self._context_manager.add_logs(incident.incident_id, logs)

            # 8. Publish event -> Orchestrator picks it up
            await self.publish(Event(
                type        = EventType.INCIDENT_CREATED,
                source      = self.name,
                incident_id = incident.incident_id,
                data        = self._incident_payload(incident),
            ))

            self.logger.warning(
                "[MonitoringAgent] INCIDENT CREATED: %s [%s] — %s",
                incident.incident_id,
                incident.severity.value.upper(),
                incident.service,
            )

            # Log files to fix
            files_to_fix = incident.metadata.get("llm_analysis", {}).get("files_to_fix", [])
            if files_to_fix:
                self.logger.warning("[MonitoringAgent] FILES TO FIX (%d):", len(files_to_fix))
                for i, f in enumerate(files_to_fix, 1):
                    self.logger.warning(
                        "[MonitoringAgent]   [%d] %s line %s in %s() — %s",
                        i, f.get("file", "?"), f.get("line", "?"),
                        f.get("function", "?"), f.get("exception", ""),
                    )

        except Exception as e:
            self._service_state.setdefault(service, {})["status"] = "error"
            self._log_event(f"ERR poll error on {service}: {e}")
            self.logger.error(
                "[MonitoringAgent] Error polling %s: %s", service, e, exc_info=True,
            )

    # --------------------------------------------------------
    # Live terminal dashboard
    # --------------------------------------------------------

    async def _dashboard_loop(self) -> None:
        """Redraws the terminal dashboard every 2 seconds."""
        while True:
            try:
                await asyncio.sleep(2)
                self._redraw_dashboard()
            except asyncio.CancelledError:
                break
            except Exception:
                pass  # never let a render error kill the loop

    def _redraw_dashboard(self) -> None:
        """Build and atomically print the full dashboard frame."""
        now    = datetime.utcnow()
        uptime = int((now - self._agent_start).total_seconds())
        um, us = divmod(uptime, 60)
        uh, um = divmod(um, 60)
        W = 72

        lines: list[str] = []

        # ── Header ────────────────────────────────────────────────────────
        lines.append(f"{_BOLD}{_CYAN}{'─' * W}{_RESET}")
        lines.append(
            f"{_BOLD}{_CYAN}  AUTONOMOUS DEVOPS — MONITORING AGENT{_RESET}"
            f"   {_DIM}uptime {uh:02d}:{um:02d}:{us:02d}  polls {self._poll_count}{_RESET}"
        )
        lines.append(
            f"  {_DIM}backend={self._config.collector_backend}  "
            f"interval={self._config.poll_interval:.0f}s  "
            f"groq={'enabled' if self._analyzer.available else 'fallback'}  "
            f"services={len(self._config.services)}{_RESET}"
        )
        lines.append(f"{_BOLD}{_CYAN}{'─' * W}{_RESET}")

        # ── Service health table ──────────────────────────────────────────
        lines.append(
            f"{_BOLD}  {'SERVICE':<22} {'STATUS':<12} "
            f"{'ERR%':<8} {'LAT ms':<10} {'CPU%':<8} {'MEM%'}{_RESET}"
        )
        lines.append(f"  {'─'*22} {'─'*12} {'─'*8} {'─'*10} {'─'*8} {'─'*8}")

        for svc, st in self._service_state.items():
            status = st.get("status", "pending")
            m      = st.get("metrics", {})
            lp     = st.get("last_poll")

            if status == "healthy":
                s_str = f"{_GREEN}● HEALTHY   {_RESET}"
            elif status == "incident":
                s_str = f"{_RED}{_BOLD}▲ INCIDENT  {_RESET}"
            elif status == "error":
                s_str = f"{_YELLOW}⚠ ERROR     {_RESET}"
            else:
                s_str = f"{_DIM}○ PENDING   {_RESET}"

            age_str = f"{_DIM}{_age(lp)}{_RESET}" if lp else f"{_DIM}—{_RESET}"
            err = m.get("error_rate", 0.0)
            lat = m.get("latency_p99_ms", 0.0)
            cpu = m.get("cpu_usage", 0.0)
            mem = m.get("memory_usage", 0.0)

            ec = _RED if err > 0.20 else _YELLOW if err > 0.05 else _GREEN
            lc = _RED if lat > 1000 else _YELLOW if lat > 500 else _GREEN
            cc = _RED if cpu > 0.75 else _YELLOW if cpu > 0.50 else _GREEN
            mc = _RED if mem > 0.85 else _YELLOW if mem > 0.70 else _GREEN

            lines.append(
                f"  {_BOLD}{svc:<22}{_RESET}{s_str:<12}"
                f"  {ec}{err*100:>5.1f}%{_RESET}   "
                f"{lc}{lat:>7.0f}{_RESET}   "
                f"{cc}{cpu*100:>5.1f}%{_RESET}  "
                f"{mc}{mem*100:>5.1f}%{_RESET}"
                f"  {age_str}"
            )

        # ── Active incidents ──────────────────────────────────────────────
        lines.append(f"\n{_BOLD}{_CYAN}{'─' * W}{_RESET}")
        lines.append(f"{_BOLD}  ACTIVE INCIDENTS ({len(self._active_incidents)}){_RESET}")
        if self._active_incidents:
            for svc, inc_id in self._active_incidents.items():
                ctx = None
                if self._context_manager:
                    ctx = self._context_manager.get_context(inc_id)
                sev_str = ""
                desc    = ""
                if ctx:
                    sev_str = _sev(ctx.incident.severity.value)
                    desc    = ctx.incident.description[:55]
                lines.append(
                    f"  {_BOLD}{inc_id}{_RESET}  {sev_str}  "
                    f"{_DIM}{svc}{_RESET}  {desc}"
                )
        else:
            lines.append(f"  {_GREEN}No active incidents{_RESET}")

        # ── Event log ─────────────────────────────────────────────────────
        lines.append(f"\n{_BOLD}{_CYAN}{'─' * W}{_RESET}")
        lines.append(f"{_BOLD}  RECENT EVENTS{_RESET}")
        recent = self._event_log[-8:]
        if recent:
            for ts, msg in recent:
                lines.append(f"  {_DIM}{ts.strftime('%H:%M:%S')}{_RESET}  {msg}")
        else:
            lines.append(f"  {_DIM}Waiting for first poll...{_RESET}")

        lines.append(f"{_BOLD}{_CYAN}{'─' * W}{_RESET}")
        lines.append(
            f"  {_DIM}Ctrl+C to stop  •  "
            f"{now.strftime('%Y-%m-%d %H:%M:%S UTC')}{_RESET}"
        )

        sys.stdout.write(_CLEAR + "\n".join(lines) + "\n")
        sys.stdout.flush()

    # --------------------------------------------------------
    # Helpers
    # --------------------------------------------------------

    def _build_collector(self) -> BaseCollector:
        """Instantiate the correct collector from config."""
        backend = self._config.collector_backend

        if backend == "mock":
            return MockCollector()

        if backend == "file":
            from agents.monitoring_agent.file_collector import FileCollector
            return FileCollector(
                log_dir     = self._config.log_dir,
                log_pattern = self._config.log_pattern,
            )

        raise ValueError(
            f"Unknown collector backend: '{backend}'. "
            f"Supported: 'mock', 'file'. "
            f"Add PrometheusCollector / DatadogCollector in collector.py."
        )

    # --------------------------------------------------------
    # Introspection
    # --------------------------------------------------------

    @property
    def active_incidents(self) -> dict[str, str]:
        """Return the current service -> incident_id mapping."""
        return dict(self._active_incidents)

    def get_info(self) -> dict:
        info = super().get_info()
        info.update({
            "services"        : self._config.services,
            "poll_interval"   : self._config.poll_interval,
            "backend"         : self._config.collector_backend,
            "log_dir"         : self._config.log_dir if self._config.collector_backend == "file" else None,
            "active_incidents": self._active_incidents,
            "groq_enabled"    : self._analyzer.available,
            "groq_model"      : self._analyzer._model,
        })
        return info