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
        anomalies += self._check_latency(service, metric_map)
        anomalies += self._check_cpu(service, metric_map)
        anomalies += self._check_memory(service, metric_map)

        # --- Log checks ---
        anomalies += self._check_log_errors(service, logs)

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

    def _check_log_errors(
        self, service: str, logs: List[Log]
    ) -> List[Anomaly]:
        """
        Flag an anomaly if too many ERROR lines appear in the log batch.
        Uses a lower severity than metric checks (logs are noisy).
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

        anomaly = Anomaly(
            service       = service,
            metric_name   = "log_error_count",
            current_value = float(count),
            threshold     = float(threshold),
            severity      = severity,
            message       = (
                f"{count} ERROR log lines detected "
                f"(threshold: {threshold}) [{severity.value}]"
            ),
            evidence_logs = error_logs[:10],  # keep first 10 as evidence
        )
        return [anomaly]

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