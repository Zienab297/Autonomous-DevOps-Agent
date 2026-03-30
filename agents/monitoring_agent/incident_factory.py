"""
agents/monitoring/incident_factory.py
---------------------------------------
Converts a list of Anomaly objects into a single Incident.

Responsibilities
----------------
- Deduplicate anomalies for the same service into one Incident
- Pick the highest severity from all anomalies
- Write a clear human-readable description
- Attach raw metrics and logs as evidence

This is intentionally thin — it is a mapping layer,
not a business logic layer.
"""

import logging
from typing import List

from core.models import Incident, IncidentStatus, Log, Metric, Severity
from agents.monitoring_agent.detector import Anomaly

logger = logging.getLogger(__name__)

# Severity ordering — higher index = worse
_SEVERITY_ORDER = [
    Severity.LOW,
    Severity.MEDIUM,
    Severity.HIGH,
    Severity.CRITICAL,
]


class IncidentFactory:
    """
    Builds Incident objects from detected Anomaly lists.

    Example:
        factory = IncidentFactory()
        incident = factory.create(
            service="auth-api",
            anomalies=anomalies,
            metrics=metrics,
            logs=logs,
        )
    """

    def create(
        self,
        service  : str,
        anomalies: List[Anomaly],
        metrics  : List[Metric],
        logs     : List[Log],
    ) -> Incident:
        """
        Build one Incident from one or more Anomaly objects.

        Args:
            service:   The service that triggered the anomalies
            anomalies: Non-empty list of Anomaly objects for this service
            metrics:   Raw metrics collected this cycle (attached as evidence)
            logs:      Raw logs collected this cycle (attached as evidence)

        Returns:
            A fully formed Incident ready to be published on the EventBus.
        """
        if not anomalies:
            raise ValueError("Cannot create an Incident from an empty anomaly list")

        # Pick the worst severity across all anomalies
        severity = self._max_severity(anomalies)

        # Build a concise description
        description = self._build_description(service, anomalies)

        # Collect issue_type and flawed_file from anomalies that have them
        issue_types  = list({a.issue_type  for a in anomalies if a.issue_type  not in ("", "unknown")})
        flawed_files = list({a.flawed_file for a in anomalies if a.flawed_file != ""})

        # Also pull flawed files directly from traceback log metadata
        for log in logs:
            meta = log.metadata or {}
            if "fix_here" in meta and meta["fix_here"] not in flawed_files:
                flawed_files.append(meta["fix_here"])
            if "issue_type" in meta and meta["issue_type"] not in issue_types:
                issue_types.append(meta["issue_type"])

        primary_issue_type  = issue_types[0]  if issue_types  else "unknown"
        primary_flawed_file = flawed_files[0] if flawed_files else ""

        incident = Incident(
            service     = service,
            severity    = severity,
            description = description,
            status      = IncidentStatus.OPEN,
            metrics     = metrics,
            logs        = logs,
            metadata    = {
                "anomaly_count"  : len(anomalies),
                "issue_type"     : primary_issue_type,
                "flawed_file"    : primary_flawed_file,
                "all_issue_types": issue_types,
                "all_flawed_files": flawed_files,
                "anomaly_details": [
                    {
                        "metric"      : a.metric_name,
                        "value"       : a.current_value,
                        "threshold"   : a.threshold,
                        "severity"    : a.severity.value,
                        "message"     : a.message,
                        "issue_type"  : a.issue_type,
                        "flawed_file" : a.flawed_file,
                    }
                    for a in anomalies
                ],
            },
        )

        logger.info("[IncidentFactory] Created: %s", incident)
        return incident

    # --------------------------------------------------------
    # Helpers
    # --------------------------------------------------------

    @staticmethod
    def _max_severity(anomalies: List[Anomaly]) -> Severity:
        """Return the highest severity from a list of anomalies."""
        return max(
            anomalies,
            key=lambda a: _SEVERITY_ORDER.index(a.severity),
        ).severity

    @staticmethod
    def _build_description(service: str, anomalies: List[Anomaly]) -> str:
        """
        Build a human-readable description for the Incident.

        Single anomaly:
            auth-api — error_rate 0.4500 exceeds CRITICAL threshold 0.40

        Multiple anomalies:
            auth-api — 3 anomalies detected:
              • error_rate 0.45 exceeds CRITICAL threshold 0.40
              • latency_p99_ms 1200.00 exceeds HIGH threshold 1000.00
              • log_error_count 22 ERROR log lines detected (threshold: 5) [high]
        """
        if len(anomalies) == 1:
            return f"{service} — {anomalies[0].message}"

        lines = [f"{service} — {len(anomalies)} anomalies detected:"]
        for a in anomalies:
            lines.append(f"  • {a.message}")
        return "\n".join(lines)