"""
agents/monitoring/config.py
----------------------------
All thresholds, intervals, and data-source settings
for the Monitoring Agent.

To tune the agent, change values here — nowhere else.
"""

from dataclasses import dataclass, field
from typing import List


@dataclass
class ThresholdConfig:
    """
    Per-metric anomaly thresholds.
    The Detector compares collected values against these.
    """
    # Error / failure rates  (0.0 – 1.0)
    error_rate_critical : float = 0.40   # 40% errors  → CRITICAL
    error_rate_high     : float = 0.20   # 20% errors  → HIGH
    error_rate_medium   : float = 0.10   # 10% errors  → MEDIUM

    # Latency  (milliseconds)
    latency_critical_ms : float = 2000.0
    latency_high_ms     : float = 1000.0
    latency_medium_ms   : float = 500.0

    # CPU usage  (0.0 – 1.0)
    cpu_critical        : float = 0.90
    cpu_high            : float = 0.75
    cpu_medium          : float = 0.60

    # Memory usage  (0.0 – 1.0)
    memory_critical     : float = 0.95
    memory_high         : float = 0.85
    memory_medium       : float = 0.70

    # Minimum number of anomalous log lines to trigger an incident
    log_error_count_threshold: int = 5


@dataclass
class MonitoringConfig:
    """
    Top-level config for the Monitoring Agent.

    Example:
        config = MonitoringConfig(
            services=["auth-api", "payments-api"],
            poll_interval=15.0,
        )
    """
    # Which services to watch
    services: List[str] = field(default_factory=lambda: [
        "auth-api",
        "payments-api",
        "inventory-service",
        "notification-service",
    ])

    # How often to poll for metrics and logs (seconds)
    poll_interval: float = 30.0

    # Which collector backend to use
    # Options: "mock" | "prometheus" | "datadog" | "cloudwatch"
    collector_backend: str = "mock"

    # Prometheus base URL (used when collector_backend="prometheus")
    prometheus_url: str = "http://localhost:9090"

    # Datadog API key (used when collector_backend="datadog")
    datadog_api_key: str = ""
    datadog_app_key: str = ""

    # Anomaly detection thresholds
    thresholds: ThresholdConfig = field(default_factory=ThresholdConfig)

    # Maximum log lines to pull per service per poll cycle
    max_log_lines: int = 100

    # Minimum confidence score to create an incident (0.0 – 1.0)
    min_incident_confidence: float = 0.5