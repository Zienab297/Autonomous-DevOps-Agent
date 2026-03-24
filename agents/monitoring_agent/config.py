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
    error_rate_critical : float = 0.40   # 40% error lines  → CRITICAL
    error_rate_high     : float = 0.20   # 20% error lines  → HIGH
    error_rate_medium   : float = 0.10   # 10% error lines  → MEDIUM

    # Latency  (milliseconds) — used by Prometheus/Datadog backends
    latency_critical_ms : float = 2000.0
    latency_high_ms     : float = 1000.0
    latency_medium_ms   : float = 500.0

    # CPU usage  (0.0 – 1.0) — used by Prometheus/Datadog backends
    cpu_critical        : float = 0.90
    cpu_high            : float = 0.75
    cpu_medium          : float = 0.60

    # Memory usage  (0.0 – 1.0) — used by Prometheus/Datadog backends
    memory_critical     : float = 0.95
    memory_high         : float = 0.85
    memory_medium       : float = 0.70

    # Minimum traceback count to trigger an incident (file backend)
    # If a CI run produces even 1 traceback, fire an incident immediately.
    # Raise this if you have noisy logs with expected non-fatal exceptions.
    traceback_count_threshold: int = 1

    # Minimum number of anomalous log lines to trigger an incident (mock/live backends)
    log_error_count_threshold: int = 5


@dataclass
class MonitoringConfig:
    """
    Top-level config for the Monitoring Agent.

    Quick-start for CI/CD log monitoring:
        config = MonitoringConfig(
            services=["auth-api", "payments-api"],
            collector_backend="file",
            log_dir="logs",
        )

    Quick-start for mock/dev mode:
        config = MonitoringConfig(
            services=["auth-api"],
            collector_backend="mock",
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
    # Options: "mock" | "file" | "prometheus" | "datadog" | "cloudwatch"
    collector_backend: str = "mock"

    # ── File backend settings (collector_backend="file") ──────────────────
    # Root directory that contains per-service subdirectories of log files.
    # Layout: {log_dir}/{service_name}/*.log
    # Example: logs/auth-api/run_2024-03-24_02-13.log
    log_dir: str = "logs"

    # Glob pattern to match log files inside each service directory.
    log_pattern: str = "*.log"

    # ── Prometheus backend settings ───────────────────────────────────────
    prometheus_url: str = "http://localhost:9090"

    # ── Datadog backend settings ──────────────────────────────────────────
    datadog_api_key: str = ""
    datadog_app_key: str = ""

    # ── Detection settings ────────────────────────────────────────────────
    thresholds: ThresholdConfig = field(default_factory=ThresholdConfig)

    # Maximum log lines to pull per service per poll cycle
    max_log_lines: int = 100

    # Minimum confidence score to create an incident (0.0 – 1.0)
    min_incident_confidence: float = 0.5