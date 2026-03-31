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
        collector.collect_metrics(service) -> List[Metric]
        collector.collect_logs(service)    -> List[Log]
        detector.analyze(...)              -> List[Anomaly]
        incident_factory.create(...)       -> Incident
        groq_analyzer.analyze(...)         -> IncidentAnalysis  <- LLM step
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
from agents.monitoring_agent.file_scanner import FileScanner, RiskLevel, ScanResult

# SYNTAX_ERROR_DETECTED must be present in EventType (core/event_bus.py).
# If not yet added, we fall back to a string sentinel so nothing crashes —
# but add it to the enum for full bus routing:
#
#   class EventType(str, Enum):
#       ...
#       SYNTAX_ERROR_DETECTED = "syntax_error_detected"
#
try:
    _SYNTAX_EVENT_TYPE = EventType.SYNTAX_ERROR_DETECTED   # type: ignore[attr-defined]
except AttributeError:
    _SYNTAX_EVENT_TYPE = "syntax_error_detected"           # graceful fallback

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
_CLEAR  = "\033[2J\033[H"

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

        self._collector = collector or self._build_collector()
        self._detector  = Detector(self._config.thresholds)
        self._factory   = IncidentFactory()
        self._analyzer  = GroqAnalyzer(api_key=groq_api_key)
        self._scanner   = FileScanner(
            rollback_threshold = RiskLevel.HIGH,
            llm_analyzer       = self._analyzer,
        )
        self._cicd_agent = None   # injected via set_cicd_agent() after construction

        self._poll_task        : Optional[asyncio.Task] = None
        self._dashboard_task   : Optional[asyncio.Task] = None
        self._dashboard_paused : bool = False   # set True by ApprovalManager during input

        self._active_incidents: dict[str, str] = {}

        # Dashboard state
        self._service_state: dict[str, dict] = {
            s: {"status": "pending", "metrics": {}, "last_poll": None, "anomaly_count": 0}
            for s in self._config.services
        }
        self._event_log  : list[tuple[datetime, str]] = []
        self._poll_count : int = 0
        self._agent_start: datetime = datetime.utcnow()

    # --------------------------------------------------------
    # BaseAgent lifecycle hooks
    # --------------------------------------------------------

    async def _setup(self) -> None:
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
        pass

    # --------------------------------------------------------
    # CI/CD integration
    # --------------------------------------------------------

    async def _on_deployment_complete(self, event: Event) -> None:
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
    # CI/CD agent wiring
    # --------------------------------------------------------

    def set_cicd_agent(self, cicd_agent) -> None:
        """
        Inject the CICDAgent after both agents are constructed.
        Called by the Orchestrator during setup.

            monitoring_agent.set_cicd_agent(cicd_agent)
        """
        self._cicd_agent = cicd_agent
        self.logger.info("[MonitoringAgent] CICDAgent wired for file-scan rollback")

    # --------------------------------------------------------
    # File scanning — public entry point
    # --------------------------------------------------------

    async def scan_and_rollback_if_unsafe(
        self,
        path:        str,
        service:     str,
        version:     str        = "",
        environment: str        = "production",
        incident_id: str | None = None,
    ) -> ScanResult:
        """
        Scan an uploaded file or directory for malicious/disturbing content.
        If unsafe, publishes FILE_SCAN_FAILED and triggers a rollback via CICDAgent.

        Returns the ScanResult so the caller can inspect files_with_problems().

        Usage (from Orchestrator or directly):
            result = await monitoring_agent.scan_and_rollback_if_unsafe(
                path        = "/uploads/artifact.sh",
                service     = "auth-api",
                version     = "v1.2.3",
                environment = "production",
            )
            if not result.safe:
                print(result.files_with_problems())
        """
        self.logger.info(
            "[MonitoringAgent] Scanning: path=%s service=%s version=%s",
            path, service, version,
        )
        self._log_event(f"SCAN  {service} — {path}")

        result: ScanResult = await asyncio.to_thread(self._scanner.scan, path)

        self.logger.info(
            "[MonitoringAgent] Scan complete: %s", result
        )

        if result.safe:
            self._log_event(f"OK    {service} — scan clean ({path})")
            return result

        # ── Unsafe — build the event payload with full file details ──────────
        files_with_problems = result.files_with_problems()

        self._log_event(
            f"!!! SCAN FAIL [{result.risk_level.value.upper()}] "
            f"{service} — {len(result.findings)} finding(s) in "
            f"{len(files_with_problems)} file(s)"
        )
        self.logger.warning(
            "[MonitoringAgent] Unsafe artifact: %s  findings=%d  files=%s",
            result.summary,
            len(result.findings),
            [f["file"] for f in files_with_problems],
        )
        for fp in files_with_problems:
            self.logger.warning(
                "[MonitoringAgent]   FILE: %s  (%d finding(s))",
                fp["file"], len(fp["findings"]),
            )
            for finding in fp["findings"]:
                self.logger.warning(
                    "[MonitoringAgent]     line %-4s [%-8s] %s — %s",
                    finding["line"], finding["risk_level"],
                    finding["category"], finding["detail"],
                )

        # Publish FILE_SCAN_FAILED with full findings attached
        await self.publish(Event(
            type        = EventType.FILE_SCAN_FAILED,
            source      = self.name,
            incident_id = incident_id,
            data        = {
                "service":             service,
                "version":             version,
                "environment":         environment,
                "path":                str(path),
                "risk_level":          result.risk_level.value,
                "summary":             result.summary,
                "finding_count":       len(result.findings),
                "files_with_problems": files_with_problems,   # ← full detail
                "scanned_files":       result.scanned_files,
            },
        ))

        # Trigger rollback if CICDAgent is wired
        if self._cicd_agent:
            self.logger.warning(
                "[MonitoringAgent] Triggering rollback: service=%s version=%s env=%s",
                service, version, environment,
            )
            await self.publish(Event(
                type        = EventType.ROLLBACK_TRIGGERED,
                source      = self.name,
                incident_id = incident_id,
                data        = {
                    "service":     service,
                    "version":     version,
                    "environment": environment,
                    "reason":      f"File scan failed: {result.summary}",
                    "files_with_problems": files_with_problems,
                },
            ))
        else:
            self.logger.warning(
                "[MonitoringAgent] No CICDAgent wired — skipping rollback. "
                "Call set_cicd_agent() to enable automatic rollback."
            )

        return result

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
    # Shared analysis pipeline
    # --------------------------------------------------------

    async def _run_analysis_pipeline(
        self,
        service : str,
        logs    : list,
        metrics : Optional[list] = None,
    ) -> Optional[Incident]:
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

        # ── Promote issue_type + flawed_file directly from Anomaly objects ─────
        # IncidentFactory.anomaly_details only serialises metric/value/severity/message —
        # it does NOT include issue_type or flawed_file, so we read from the live
        # anomaly list we already have in scope instead.
        # Priority order: syntax > import > runtime > cicd_failure > unknown
        _PRIORITY = {"syntax": 4, "import": 3, "runtime": 2, "cicd_failure": 1, "unknown": 0}
        best_issue_type  = "unknown"
        best_flawed_file = ""
        for a in anomalies:
            it = getattr(a, "issue_type", "unknown") or "unknown"
            ff = getattr(a, "flawed_file", "") or ""
            if _PRIORITY.get(it, 0) > _PRIORITY.get(best_issue_type, 0):
                best_issue_type  = it
                best_flawed_file = ff
            elif it == best_issue_type and ff and not best_flawed_file:
                best_flawed_file = ff

        incident.metadata["issue_type"]  = best_issue_type
        incident.metadata["flawed_file"] = best_flawed_file
        incident.metadata["all_issue_types"]  = list({
            getattr(a, "issue_type", "unknown") for a in anomalies
        })
        incident.metadata["all_flawed_files"] = list({
            getattr(a, "flawed_file", "")
            for a in anomalies
            if getattr(a, "flawed_file", "")
        })

        return incident

    @staticmethod
    def _strings_to_logs(raw_lines: List[str], service: str) -> list:
        """
        Convert raw CI/CD log strings to Log objects the Detector can process.

        Handles:
          1. Free-text logs — keyword match for ERROR/FAIL/TRACEBACK etc.
          2. Structured CI/CD step summaries — conclusion=failure/skipped
          3. Inline syntax errors with file+line on the same line:
               *** Sorry: IndentationError: ... (main.py, line 11)
               SyntaxError: invalid syntax (deploy.py, line 5)
          4. Multi-line compiler blocks where file+line are on a PRECEDING line:
               ***   File "./main.py", line 15
                   uvicorn.run(app, host="0.0.0.0", port=8000)?
                                                              ^
               SyntaxError: invalid syntax
             → regex lookback first, then LLM fallback if regex misses.
        """
        import re as _re
        import json as _json

        _INLINE_SYN = _re.compile(
            r'(?:^\*+\s*Sorry:\s*|##\[error\]\s*|^\s*E\s+)?' +
            r'(?P<exc>SyntaxError|IndentationError|TabError):\s*' +
            r'(?P<msg>[^(]+?)\s*' +
            r'\((?P<file>[^,)]+),\s*line\s+(?P<line>\d+)\)',
            _re.IGNORECASE,
        )

        # Matches "***   File \"./main.py\", line 15" — with or without *** prefix
        _FILE_HDR = _re.compile(
            r'^(?:\*+\s*)?\s*File\s+\"(?P<file>[^\"]+)\",\s+line\s+(?P<line>\d+)\s*$'
        )

        def _llm_extract_location(context_lines, exc_msg):
            """Ask the LLM for file+line when regex can\'t find them."""
            try:
                from agents.monitoring_agent.groq_analyzer import _chat
                context_block = "\n".join(l.strip() for l in context_lines[-10:])
                prompt = (
                    "You are a CI/CD log parser. "
                    "Extract the Python source file name and line number from this syntax error block. "
                    "Respond with ONLY valid JSON, no explanation: "
                    '{"file": "main.py", "line": 15}\n\n' +
                    f"Error: {exc_msg}\n\nContext lines:\n{context_block}"
                )
                raw = _chat(prompt)
                b_start = raw.find("{")
                b_end   = raw.rfind("}")
                if b_start == -1 or b_end == -1:
                    return {}
                parsed = _json.loads(raw[b_start : b_end + 1])
                file_ = str(parsed.get("file", "")).strip().lstrip("./")
                line_ = int(parsed.get("line", 0))
                if file_ and line_:
                    logger.debug(
                        "[MonitoringAgent] LLM extracted location: %s:%s", file_, line_
                    )
                    return {"file": file_, "line": line_}
            except Exception as _e:
                logger.debug("[MonitoringAgent] LLM location extraction failed: %s", _e)
            return {}

        logs = []
        for i, line in enumerate(raw_lines):
            msg   = line.strip()
            lower = msg.lower()
            upper = msg.upper()

            is_gh_error   = msg.startswith("##[error]") or "##[error]" in lower
            is_gh_warning = msg.startswith("##[warning]") or "##[warning]" in lower
            is_exit_fail  = (
                "process completed with exit code" in lower
                and not lower.endswith("exit code 0")
            )
            is_conclusion_fail = (
                "conclusion=failure" in lower or "conclusion=skipped" in lower
            )

            if is_gh_error or is_exit_fail or is_conclusion_fail:
                level = "ERROR"
            elif is_gh_warning:
                level = "WARN"
            elif any(k in upper for k in ("ERROR", "FAIL", "TRACEBACK", "EXCEPTION", "CRITICAL")):
                level = "ERROR"
            elif any(k in upper for k in ("WARNING", "WARN")):
                level = "WARN"
            else:
                level = "INFO"

            meta: dict = {}

            # ── Priority 1: inline syntax error — file+line on same line ────
            inline_m = _INLINE_SYN.search(msg)
            if inline_m:
                level              = "ERROR"
                file_name          = inline_m.group("file").strip()
                line_no            = inline_m.group("line")
                exc_name           = inline_m.group("exc")
                meta["issue_type"] = "syntax"
                meta["exception"]  = exc_name
                meta["file"]       = file_name
                meta["line"]       = int(line_no)
                meta["fix_here"]   = f"{file_name}:{line_no}"
                meta["full_traceback"] = msg

            # ── Priority 2: bare syntax keyword — no file+line on this line ─
            # Strategy: regex lookback → LLM fallback → bare incident (no location)
            elif any(e in lower for e in ("syntaxerror", "indentationerror", "taberror")):
                level              = "ERROR"
                meta["issue_type"] = "syntax"
                exc_name           = next(
                    (e for e in ("SyntaxError", "IndentationError", "TabError")
                     if e.lower() in lower),
                    "SyntaxError",
                )
                meta["exception"] = exc_name

                preceding = raw_lines[max(0, i - 10): i]

                # a) regex: scan preceding lines for File "x.py", line N header
                found_file = found_line = None
                for prev in reversed(preceding):
                    prev_s = prev.strip()
                    if not prev_s or set(prev_s) <= {"^", " ", "\t"}:
                        continue          # skip blank / caret lines
                    fm = _FILE_HDR.match(prev)
                    if fm:
                        found_file = fm.group("file").strip().lstrip("./")
                        found_line = int(fm.group("line"))
                        break
                    # keep scanning past source-code snippet lines

                if found_file and found_line:
                    meta["file"]     = found_file
                    meta["line"]     = found_line
                    meta["fix_here"] = f"{found_file}:{found_line}"
                    meta["full_traceback"] = (
                        "\n".join(l.strip() for l in preceding[-5:]) + f"\n{msg}"
                    )
                else:
                    # b) LLM fallback — give it the context window
                    loc = _llm_extract_location(preceding, exc_msg=msg)
                    if loc:
                        meta["file"]     = loc["file"]
                        meta["line"]     = loc["line"]
                        meta["fix_here"] = f"{loc['file']}:{loc['line']}"
                        meta["full_traceback"] = (
                            "\n".join(l.strip() for l in preceding[-5:]) + f"\n{msg}"
                        )
                    # c) last resort: inline file ref anywhere in the line
                    if "fix_here" not in meta:
                        _fn = _re.search(
                            r'file\s+"?([^\s",]+\.py)"?,\s*line\s+(\d+)', lower
                        )
                        if _fn:
                            meta["file"]     = _fn.group(1)
                            meta["line"]     = int(_fn.group(2))
                            meta["fix_here"] = f"{_fn.group(1)}:{_fn.group(2)}"

            elif any(e in lower for e in ("modulenotfounderror", "importerror")):
                meta["issue_type"] = "import"

            logs.append(Log(
                message   = msg,
                level     = level,
                service   = service,
                timestamp = datetime.utcnow(),
                metadata  = meta,
            ))
        return logs
    @staticmethod
    def _incident_payload(incident: Incident) -> dict:
        llm  = incident.metadata.get("llm_analysis", {})
        meta = incident.metadata

        # Build a clean syntax_errors list from anomaly_details for easy consumption.
        # Includes error_type + raw_message so _on_syntax_error() in SelfHealingAgent
        # can build a correct FileToFix without needing to re-parse the message.
        syntax_errors = [
            {
                "file"       : d.get("flawed_file", "").split(":")[0],
                "line"       : d.get("flawed_file", "").split(":")[1] if ":" in d.get("flawed_file", "") else "?",
                "error_type" : d.get("metric", "syntax_error"),
                "message"    : d.get("message", ""),
                "raw_message": d.get("message", ""),
            }
            for d in meta.get("anomaly_details", [])
            if d.get("issue_type") == "syntax" and d.get("flawed_file")
        ]

        # Human-readable label for orchestrator/dashboard display
        _ISSUE_LABELS = {
            "syntax"      : "SYNTAX ERROR",
            "import"      : "IMPORT ERROR",
            "runtime"     : "RUNTIME ERROR",
            "cicd_failure": "CI/CD PIPELINE FAILURE",
            "unknown"     : "UNCLASSIFIED ERROR",
        }
        raw_issue_type  = meta.get("issue_type", "unknown") or "unknown"
        issue_type_label = _ISSUE_LABELS.get(raw_issue_type, raw_issue_type.upper().replace("_", " "))

        return {
            "incident_id"       : incident.incident_id,
            "service"           : incident.service,
            "severity"          : incident.severity.value,
            "description"       : incident.description,
            "impact"            : llm.get("impact", ""),
            "recommended"       : llm.get("recommended", ""),
            "confidence"        : llm.get("confidence", 0.0),
            "report"            : llm.get("report", ""),
            "anomaly_count"     : meta.get("anomaly_count", 0),
            "llm_fallback"      : llm.get("fallback", True),
            "files_to_fix"      : llm.get("files_to_fix", []),
            # ── issue classification ───────────────────────────────────────
            "issue_type"        : raw_issue_type,
            "issue_type_label"  : issue_type_label,   # ← for display in orchestrator/UI
            "flawed_file"       : meta.get("flawed_file", ""),
            "all_issue_types"   : meta.get("all_issue_types", []),
            "all_flawed_files"  : meta.get("all_flawed_files", []),
            # ── syntax errors — always present, empty list if none ─────────
            "syntax_errors"     : syntax_errors,
            "has_syntax_error"  : bool(syntax_errors),
        }

    def _log_event(self, msg: str) -> None:
        self._event_log.append((datetime.utcnow(), msg))
        if len(self._event_log) > 50:
            self._event_log.pop(0)

    # --------------------------------------------------------
    # Polling loop
    # --------------------------------------------------------

    async def _poll_loop(self) -> None:
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
                    "[MonitoringAgent] Unexpected error in poll loop: %s", e, exc_info=True,
                )

    async def _poll_all_services(self) -> None:
        tasks = [self._poll_service(s) for s in self._config.services]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _poll_service(self, service: str) -> None:
        try:
            metrics = await self._collector.collect_metrics(service)
            logs    = await self._collector.collect_logs(
                service, max_lines=self._config.max_log_lines
            )

            self._poll_count += 1
            self._service_state[service]["last_poll"] = datetime.utcnow()
            self._service_state[service]["metrics"]   = {m.name: m.value for m in metrics}

            anomalies = self._detector.analyze(service, metrics, logs)

            if not anomalies:
                if service in self._active_incidents:
                    self._log_event(f"OK  {service} — returned to healthy")
                    del self._active_incidents[service]
                self._service_state[service]["status"]        = "healthy"
                self._service_state[service]["anomaly_count"] = 0
                return

            if service in self._active_incidents:
                return

            incident = await self._run_analysis_pipeline(
                service = service, logs = logs, metrics = metrics,
            )
            if not incident:
                return

            self._active_incidents[service] = incident.incident_id
            self._service_state[service]["status"]        = "incident"
            self._service_state[service]["anomaly_count"] = len(anomalies)
            self._log_event(
                f"!!! INCIDENT {incident.incident_id} "
                f"[{incident.severity.value.upper()}] — {service}"
            )

            if self._state_manager:
                self._state_manager.add_incident(incident)
            if self._context_manager:
                self._context_manager.create_context(incident)
                self._context_manager.add_metrics(incident.incident_id, metrics)
                self._context_manager.add_logs(incident.incident_id, logs)

            await self.publish(Event(
                type        = EventType.INCIDENT_CREATED,
                source      = self.name,
                incident_id = incident.incident_id,
                data        = self._incident_payload(incident),
            ))

            self.logger.warning(
                "[MonitoringAgent] INCIDENT CREATED: %s [%s] — %s",
                incident.incident_id, incident.severity.value.upper(), incident.service,
            )

            # ── Broadcast a dedicated SYNTAX_ERROR_DETECTED event so every
            #    agent in the system learns the exact broken file + line ─────
            syntax_anomalies = [
                a for a in anomalies
                if getattr(a, "issue_type", "") == "syntax"
            ]
            if syntax_anomalies:
                syntax_files = []
                for a in syntax_anomalies:
                    parts     = a.flawed_file.split(":", 1)
                    file_name = parts[0]
                    line_no   = parts[1] if len(parts) > 1 else "?"
                    syntax_files.append({
                        "file"       : file_name,
                        "line"       : line_no,
                        "message"    : a.message,
                        "incident_id": incident.incident_id,
                    })

                await self.publish(Event(
                    type        = _SYNTAX_EVENT_TYPE,
                    source      = self.name,
                    incident_id = incident.incident_id,
                    data        = {
                        "service"      : service,
                        "incident_id"  : incident.incident_id,
                        "severity"     : "high",
                        "error_count"  : len(syntax_files),
                        "syntax_errors": syntax_files,
                        "summary"      : (
                            f"{len(syntax_files)} syntax error(s) in {service} — "
                            + ", ".join(
                                f"{f['file']}:{f['line']}" for f in syntax_files
                            )
                        ),
                    },
                ))
                self.logger.error(
                    "[MonitoringAgent] 🔴 SYNTAX_ERROR_DETECTED — %d broken file(s) in '%s': %s",
                    len(syntax_files),
                    service,
                    [f"{f['file']}:{f['line']}" for f in syntax_files],
                )

            # Log issue_type and flawed_file from incident metadata
            issue_type  = incident.metadata.get("issue_type", "unknown")
            flawed_file = incident.metadata.get("flawed_file", "")
            if issue_type != "unknown" or flawed_file:
                self.logger.warning(
                    "[MonitoringAgent] ISSUE TYPE: %s%s",
                    issue_type.upper(),
                    f" — flawed file: {flawed_file}" if flawed_file else "",
                )
                if issue_type == "syntax" and flawed_file:
                    self.logger.warning(
                        "[MonitoringAgent] SYNTAX ERROR in %s — fix this file before redeploying",
                        flawed_file,
                    )

            files_to_fix = incident.metadata.get("llm_analysis", {}).get("files_to_fix", [])
            if files_to_fix:
                self.logger.warning("[MonitoringAgent] FILES TO FIX (%d):", len(files_to_fix))
                for idx, f in enumerate(files_to_fix, 1):
                    self.logger.warning(
                        "[MonitoringAgent]   [%d] %s line %s in %s() — %s [%s]",
                        idx, f.get("file","?"), f.get("line","?"),
                        f.get("function","?"), f.get("exception",""),
                        f.get("issue_type", issue_type),
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
        while True:
            try:
                await asyncio.sleep(2)
                if not self._dashboard_paused:
                    self._redraw_dashboard()
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    def pause_dashboard(self) -> None:
        """
        Stop the live dashboard from printing.
        Called by ApprovalManager before showing the CLI input prompt
        so background prints don't overwrite what the user is typing.
        """
        self._dashboard_paused = True

    def resume_dashboard(self) -> None:
        """Resume the live dashboard after the user has answered."""
        self._dashboard_paused = False

    def _redraw_dashboard(self) -> None:
        now    = datetime.utcnow()
        uptime = int((now - self._agent_start).total_seconds())
        um, us = divmod(uptime, 60)
        uh, um = divmod(um, 60)
        W = 72
        lines: list[str] = []

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