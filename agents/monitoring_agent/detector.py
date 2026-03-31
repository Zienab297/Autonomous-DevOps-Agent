"""
agents/monitoring/detector.py
-------------------------------
Inspects collected metrics and logs and decides whether
an anomaly has occurred — and if so, how severe it is.

Design
------
- Pure functions: no I/O, no async, easy to unit-test.
- Returns a list of Anomaly dataclasses (empty = all clear).
- The MonitoringAgent passes anomalies to the IncidentFactory.

Extend this module to add:
- Rolling window / trend detection
- Statistical baselines (z-score, IQR)
- ML-based models
without changing anything in agent.py.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

from core.models import Log, Metric, Severity
from agents.monitoring_agent.config import ThresholdConfig

logger = logging.getLogger(__name__)


# ============================================================
# Anomaly — what the detector reports back
# ============================================================

@dataclass
class Anomaly:
    """
    Represents a single detected anomaly.

    Produced by Detector.analyze() and consumed by IncidentFactory.

    Example:
        Anomaly(
            service="auth-api",
            metric_name="error_rate",
            current_value=0.45,
            threshold=0.40,
            severity=Severity.CRITICAL,
            message="error_rate 0.4500 exceeds CRITICAL threshold 0.40",
            issue_type="runtime",
            flawed_file="deploy.py:47",
        )
    """
    service       : str
    metric_name   : str
    current_value : float
    threshold     : float
    severity      : Severity
    message       : str
    detected_at   : datetime = field(default_factory=datetime.utcnow)

    # Optional: raw evidence that triggered the anomaly
    evidence_logs : List[Log] = field(default_factory=list)

    # Issue classification — populated when traceback logs are present
    issue_type    : str = "unknown"   # "syntax" | "import" | "runtime" | "unknown"
    flawed_file   : str = ""          # e.g. "deploy.py:47" or "" if not available

    def __str__(self):
        return (
            f"Anomaly({self.service} | {self.metric_name}={self.current_value:.4f} "
            f"[{self.severity.value}])"
        )


# ============================================================
# Detector
# ============================================================

class Detector:
    """
    Inspects metrics and logs and returns a list of Anomaly objects.

    Usage:
        detector = Detector(thresholds)
        anomalies = detector.analyze(service, metrics, logs)
        if anomalies:
            # hand to IncidentFactory
    """

    def __init__(self, thresholds: ThresholdConfig):
        self._t = thresholds

    def analyze(
        self,
        service  : str,
        metrics  : List[Metric],
        logs     : List[Log],
    ) -> List[Anomaly]:
        """
        Run all detection checks against the collected data.

        Returns:
            List of Anomaly objects. Empty list means all clear.
        """
        anomalies: List[Anomaly] = []

        # Index metrics by name for O(1) lookup
        metric_map = {m.name: m for m in metrics}

        # --- Metric checks ---
        anomalies += self._check_error_rate(service, metric_map)
        anomalies += self._check_traceback_count(service, metric_map)  # file backend
        anomalies += self._check_latency(service, metric_map)
        anomalies += self._check_cpu(service, metric_map)
        anomalies += self._check_memory(service, metric_map)

        # --- Log checks ---
        anomalies += self._check_syntax_errors(service, logs)   # ← syntax first
        anomalies += self._check_log_errors(service, logs)
        anomalies += self._check_cicd_conclusion(service, logs)

        if anomalies:
            logger.warning(
                "[Detector] %d anomaly(ies) detected for %s: %s",
                len(anomalies),
                service,
                [str(a) for a in anomalies],
            )
        else:
            logger.debug("[Detector] %s — all clear", service)

        return anomalies

    # --------------------------------------------------------
    # Private — individual metric checks
    # --------------------------------------------------------

    def _check_error_rate(
        self, service: str, metrics: dict[str, Metric]
    ) -> List[Anomaly]:
        m = metrics.get("error_rate")
        if m is None:
            return []

        v = m.value
        t = self._t

        if v >= t.error_rate_critical:
            return [self._anomaly(service, "error_rate", v, t.error_rate_critical, Severity.CRITICAL)]
        if v >= t.error_rate_high:
            return [self._anomaly(service, "error_rate", v, t.error_rate_high, Severity.HIGH)]
        if v >= t.error_rate_medium:
            return [self._anomaly(service, "error_rate", v, t.error_rate_medium, Severity.MEDIUM)]
        return []

    def _check_traceback_count(
        self, service: str, metrics: dict[str, Metric]
    ) -> List[Anomaly]:
        """
        Check the traceback_count metric produced by FileCollector.
        Any CI run with >= threshold tracebacks is an incident.

        Even 1 traceback is enough to fire at MEDIUM severity — this is a
        real code bug, not a noisy log line.
        """
        m = metrics.get("traceback_count")
        if m is None:
            return []

        count = int(m.value)
        threshold = self._t.traceback_count_threshold

        if count < threshold:
            return []

        # Scale severity with count
        if count >= 10:
            severity = Severity.CRITICAL
        elif count >= 5:
            severity = Severity.HIGH
        elif count >= 2:
            severity = Severity.MEDIUM
        else:
            severity = Severity.MEDIUM  # even 1 traceback = MEDIUM

        return [Anomaly(
            service       = service,
            metric_name   = "traceback_count",
            current_value = float(count),
            threshold     = float(threshold),
            severity      = severity,
            message       = (
                f"{count} traceback(s) detected in CI/CD logs "
                f"(threshold: {threshold}) [{severity.value}]"
            ),
            issue_type    = "runtime",   # refined by _check_log_errors if logs present
            flawed_file   = "",          # populated downstream from log metadata
        )]

    def _check_latency(
        self, service: str, metrics: dict[str, Metric]
    ) -> List[Anomaly]:
        m = metrics.get("latency_p99_ms")
        if m is None:
            return []

        v = m.value
        t = self._t

        if v >= t.latency_critical_ms:
            return [self._anomaly(service, "latency_p99_ms", v, t.latency_critical_ms, Severity.CRITICAL)]
        if v >= t.latency_high_ms:
            return [self._anomaly(service, "latency_p99_ms", v, t.latency_high_ms, Severity.HIGH)]
        if v >= t.latency_medium_ms:
            return [self._anomaly(service, "latency_p99_ms", v, t.latency_medium_ms, Severity.MEDIUM)]
        return []

    def _check_cpu(
        self, service: str, metrics: dict[str, Metric]
    ) -> List[Anomaly]:
        m = metrics.get("cpu_usage")
        if m is None:
            return []

        v = m.value
        t = self._t

        if v >= t.cpu_critical:
            return [self._anomaly(service, "cpu_usage", v, t.cpu_critical, Severity.CRITICAL)]
        if v >= t.cpu_high:
            return [self._anomaly(service, "cpu_usage", v, t.cpu_high, Severity.HIGH)]
        if v >= t.cpu_medium:
            return [self._anomaly(service, "cpu_usage", v, t.cpu_medium, Severity.MEDIUM)]
        return []

    def _check_memory(
        self, service: str, metrics: dict[str, Metric]
    ) -> List[Anomaly]:
        m = metrics.get("memory_usage")
        if m is None:
            return []

        v = m.value
        t = self._t

        if v >= t.memory_critical:
            return [self._anomaly(service, "memory_usage", v, t.memory_critical, Severity.CRITICAL)]
        if v >= t.memory_high:
            return [self._anomaly(service, "memory_usage", v, t.memory_high, Severity.HIGH)]
        if v >= t.memory_medium:
            return [self._anomaly(service, "memory_usage", v, t.memory_medium, Severity.MEDIUM)]
        return []

    def _check_syntax_errors(
        self, service: str, logs: List[Log]
    ) -> List[Anomaly]:
        """
        Scan logs for Python syntax/indentation errors that were parsed by
        LogParser._match_gha_syntax_error() and tagged with issue_type="syntax"
        and fix_here=<file>:<line> in their metadata.

        One Anomaly per unique broken file.
        """
        anomalies: List[Anomaly] = []
        seen_files: set = set()

        for log in logs:
            meta = log.metadata or {}
            if meta.get("issue_type") != "syntax":
                continue

            exc_type = meta.get("exception", "SyntaxError")
            fix_here  = meta.get("fix_here", "")
            file_name = meta.get("file", "")
            line_no   = meta.get("line", "?")

            # One anomaly per unique file — avoid duplicate noise
            if fix_here and fix_here in seen_files:
                continue
            if fix_here:
                seen_files.add(fix_here)

            location = f"{file_name}:{line_no}" if file_name else fix_here or "unknown"
            message  = (
                f"SYNTAX ERROR in {location} — {exc_type}: {log.message} "
                f"[fix this file before redeploying]"
            )

            anomalies.append(Anomaly(
                service       = service,
                metric_name   = "syntax_error",
                current_value = 1.0,
                threshold     = 0.0,
                severity      = Severity.HIGH,   # always HIGH — service won't boot
                message       = message,
                evidence_logs = [log],
                issue_type    = "syntax",
                flawed_file   = fix_here or location,
            ))

        if anomalies:
            logger.error(
                "[Detector] %d SYNTAX ERROR(s) in %s — files: %s",
                len(anomalies),
                service,
                [a.flawed_file for a in anomalies],
            )

        return anomalies

    def _check_log_errors(
        self, service: str, logs: List[Log]
    ) -> List[Anomaly]:
        """
        Flag an anomaly if too many ERROR lines appear in the log batch.

        For FileCollector, every Log is already a traceback — so this acts
        as a secondary confirmation check. For MockCollector / live backends
        it's the primary log-level check.

        Also extracts issue_type and flawed_file from log metadata when
        available (populated by FileCollector from parsed tracebacks).
        """
        error_logs = [l for l in logs if l.level == "ERROR"]
        count = len(error_logs)
        threshold = self._t.log_error_count_threshold

        if count < threshold:
            return []

        # Severity scales with error count
        if count >= threshold * 4:
            severity = Severity.CRITICAL
        elif count >= threshold * 2:
            severity = Severity.HIGH
        else:
            severity = Severity.MEDIUM

        # Extract issue_type and flawed_file from error log metadata.
        # Priority 1: a log that has fix_here (exact location known)
        # Priority 2: a log that has issue_type but no fix_here
        #             (e.g. ##[error]IndentationError from GitHub Actions)
        issue_type  = "unknown"
        flawed_file = ""
        fallback_issue_type = "unknown"

        for log in error_logs:
            meta = log.metadata or {}
            if "fix_here" in meta:
                # Best case — we know the exact file and line
                issue_type  = meta.get("issue_type", "runtime")
                flawed_file = meta.get("fix_here", "")
                break
            elif meta.get("issue_type") and meta["issue_type"] != "unknown":
                # We know the type but not the exact location yet
                fallback_issue_type = meta["issue_type"]

        # Use the fallback type if we never found a fix_here
        if issue_type == "unknown" and fallback_issue_type != "unknown":
            issue_type = fallback_issue_type

        anomaly = Anomaly(
            service       = service,
            metric_name   = "log_error_count",
            current_value = float(count),
            threshold     = float(threshold),
            severity      = severity,
            message       = (
                f"{count} ERROR log lines detected "
                f"(threshold: {threshold}) [{severity.value}]"
                + (f" — {issue_type} error in {flawed_file}" if flawed_file else "")
            ),
            evidence_logs = error_logs[:10],
            issue_type    = issue_type,
            flawed_file   = flawed_file,
        )
        return [anomaly]

    def _check_cicd_conclusion(
        self, service: str, logs: List[Log]
    ) -> List[Anomaly]:
        """
        Detects CI/CD pipeline failures from structured step summary lines.

        GitHub Actions / CI agents emit lines like:
            conclusion=failure
            conclusion=skipped   (skipped because a prior step failed)
            status=completed conclusion=failure

        These don't look like ERROR log lines but DO indicate a real failure.
        This check fires even with just 1 failure line — CI/CD failures are
        always worth an incident regardless of total log volume.
        """
        failure_logs  = []
        skipped_logs  = []
        pipeline_failed = False

        for log in logs:
            msg       = log.message
            msg_lower = msg.lower()

            # Top-level pipeline failure (CI/CD agent structured summary)
            if "status=completed" in msg_lower and "conclusion=failure" in msg_lower:
                pipeline_failed = True
                failure_logs.append(log)

            # Individual step failure — structured summary
            elif "conclusion=failure" in msg_lower:
                failure_logs.append(log)

            # GitHub Actions native error lines: ##[error]<message>
            elif msg.startswith("##[error]") or "##[error]" in msg_lower:
                failure_logs.append(log)

            # Non-zero exit code line from GitHub Actions runner
            elif (
                "process completed with exit code" in msg_lower
                and not msg_lower.endswith("exit code 0")
            ):
                pipeline_failed = True
                failure_logs.append(log)

            # Skipped steps — secondary signal
            elif "conclusion=skipped" in msg_lower:
                skipped_logs.append(log)

        if not failure_logs:
            return []

        # Severity: pipeline-level failure is HIGH, step-level is MEDIUM
        severity = Severity.HIGH if pipeline_failed else Severity.MEDIUM

        failed_steps = [
            l.message.strip() for l in failure_logs[:5]
        ]
        skipped_count = len(skipped_logs)

        message = (
            f"CI/CD pipeline failure: {len(failure_logs)} step(s) failed"
            + (f", {skipped_count} skipped" if skipped_count else "")
            + f" [{severity.value}]"
        )

        # Try to find a syntax/import/runtime error hidden inside the failed step logs
        # so we can classify more precisely than "cicd_failure"
        refined_issue_type = "cicd_failure"
        for log in failure_logs + skipped_logs:
            meta = log.metadata or {}
            it = meta.get("issue_type", "")
            if it in ("syntax", "import", "runtime"):
                refined_issue_type = it
                break
            msg_lower = log.message.lower()
            if any(e in msg_lower for e in ("syntaxerror", "indentationerror", "taberror")):
                refined_issue_type = "syntax"
                break
            if any(e in msg_lower for e in ("modulenotfounderror", "importerror")):
                refined_issue_type = "import"
                break

        # ── FIX: extract flawed_file from failure log metadata ────────────────
        # Previously this was always left blank, causing the anomaly to have
        # issue_type="syntax" but flawed_file="" — so SYNTAX_ERROR_DETECTED
        # fired with an empty file field and the self-healing agent skipped it.
        refined_flawed_file = ""
        for log in failure_logs:
            meta = log.metadata or {}
            fh = meta.get("fix_here", "")
            if fh:
                refined_flawed_file = fh
                break
            # Fallback: reconstruct from file + line keys if fix_here absent
            if meta.get("file"):
                line = meta.get("line", "?")
                refined_flawed_file = f"{meta['file']}:{line}"
                break

        return [Anomaly(
            service       = service,
            metric_name   = "cicd_conclusion",
            current_value = float(len(failure_logs)),
            threshold     = 1.0,
            severity      = severity,
            message       = message,
            evidence_logs = (failure_logs + skipped_logs)[:10],
            issue_type    = refined_issue_type,
            flawed_file   = refined_flawed_file,   # ← was always "" before this fix
        )]

    # --------------------------------------------------------
    # Helper
    # --------------------------------------------------------

    @staticmethod
    def _anomaly(
        service    : str,
        metric_name: str,
        value      : float,
        threshold  : float,
        severity   : Severity,
    ) -> Anomaly:
        return Anomaly(
            service       = service,
            metric_name   = metric_name,
            current_value = value,
            threshold     = threshold,
            severity      = severity,
            message       = (
                f"{metric_name} {value:.4f} exceeds "
                f"{severity.value.upper()} threshold {threshold:.4f}"
            ),
        )