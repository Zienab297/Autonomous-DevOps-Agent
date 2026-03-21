"""
agents/monitoring/collector.py
--------------------------------
Pulls metrics and logs from a data source.

Architecture
------------
BaseCollector  — abstract interface (swap backends without touching the agent)
MockCollector  — deterministic fake data for development and testing
                 (inject anomalies via MockCollector.inject_anomaly())

To add a real backend later:
    class PrometheusCollector(BaseCollector): ...
    class DatadogCollector(BaseCollector):    ...
    class CloudWatchCollector(BaseCollector): ...
"""

import logging
import random
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import List, Optional

from core.models import Log, Metric

logger = logging.getLogger(__name__)


# ============================================================
# BaseCollector — the interface every backend must satisfy
# ============================================================

class BaseCollector(ABC):
    """
    Abstract interface for all metric and log collectors.

    Every method must be async so backends can use aiohttp,
    boto3, etc. without blocking the event loop.
    """

    @abstractmethod
    async def collect_metrics(self, service: str) -> List[Metric]:
        """
        Fetch current metrics for a service.

        Args:
            service: Name of the service to poll

        Returns:
            List of Metric objects (may be empty if the source is down)
        """

    @abstractmethod
    async def collect_logs(self, service: str, max_lines: int = 100) -> List[Log]:
        """
        Fetch recent log lines for a service.

        Args:
            service:   Name of the service to poll
            max_lines: Maximum number of log lines to return

        Returns:
            List of Log objects (may be empty)
        """

    async def health_check(self) -> bool:
        """
        Verify the collector can reach its data source.
        Override in real backends.
        """
        return True


# ============================================================
# MockCollector — deterministic fake data
# ============================================================

class MockCollector(BaseCollector):
    """
    Returns synthetic metrics and logs.
    Useful for development, CI, and unit tests.

    Normal mode: healthy values with small random variance.
    Anomaly mode: inject a spike for a specific service.

    Example:
        collector = MockCollector(seed=42)

        # Trigger a high-error-rate anomaly on auth-api
        collector.inject_anomaly("auth-api", "error_rate", value=0.45)

        metrics = await collector.collect_metrics("auth-api")
        # → error_rate metric will be 0.45
    """

    def __init__(self, seed: Optional[int] = None):
        self._rng = random.Random(seed)
        # service → { metric_name → forced_value }
        self._injected: dict[str, dict[str, float]] = {}
        logger.info("[MockCollector] Initialized (seed=%s)", seed)

    # --------------------------------------------------------
    # Anomaly injection (for testing)
    # --------------------------------------------------------

    def inject_anomaly(
        self,
        service: str,
        metric_name: str,
        value: float,
    ) -> None:
        """
        Force a specific metric value for the next poll cycle.
        Call clear_anomaly() to return to normal.

        Example:
            collector.inject_anomaly("auth-api", "error_rate", 0.45)
        """
        if service not in self._injected:
            self._injected[service] = {}
        self._injected[service][metric_name] = value
        logger.info(
            "[MockCollector] Injected anomaly: %s.%s = %.3f",
            service, metric_name, value,
        )

    def clear_anomaly(self, service: str, metric_name: Optional[str] = None) -> None:
        """
        Remove injected anomaly.
        Pass metric_name=None to clear all anomalies for the service.
        """
        if service in self._injected:
            if metric_name:
                self._injected[service].pop(metric_name, None)
            else:
                del self._injected[service]

    def clear_all_anomalies(self) -> None:
        """Remove all injected anomalies."""
        self._injected.clear()

    # --------------------------------------------------------
    # BaseCollector implementation
    # --------------------------------------------------------

    async def collect_metrics(self, service: str) -> List[Metric]:
        """
        Return synthetic metrics for a service.
        Respects any injected anomalies.
        """
        injected = self._injected.get(service, {})
        now = datetime.utcnow()

        def val(name: str, default: float, noise: float = 0.02) -> float:
            """Return injected value if present, else default ± noise."""
            if name in injected:
                return injected[name]
            return max(0.0, default + self._rng.uniform(-noise, noise))

        metrics = [
            Metric(
                name      = "error_rate",
                value     = round(val("error_rate", 0.02, 0.01), 4),
                unit      = "%",
                service   = service,
                timestamp = now,
                labels    = {"env": "prod"},
            ),
            Metric(
                name      = "latency_p99_ms",
                value     = round(val("latency_p99_ms", 120.0, 20.0), 2),
                unit      = "ms",
                service   = service,
                timestamp = now,
                labels    = {"env": "prod"},
            ),
            Metric(
                name      = "cpu_usage",
                value     = round(val("cpu_usage", 0.35, 0.05), 4),
                unit      = "%",
                service   = service,
                timestamp = now,
                labels    = {"env": "prod"},
            ),
            Metric(
                name      = "memory_usage",
                value     = round(val("memory_usage", 0.50, 0.05), 4),
                unit      = "%",
                service   = service,
                timestamp = now,
                labels    = {"env": "prod"},
            ),
            Metric(
                name      = "request_rate",
                value     = round(val("request_rate", 250.0, 30.0), 1),
                unit      = "req/s",
                service   = service,
                timestamp = now,
                labels    = {"env": "prod"},
            ),
        ]

        logger.debug("[MockCollector] Collected %d metrics for %s", len(metrics), service)
        return metrics

    async def collect_logs(self, service: str, max_lines: int = 100) -> List[Log]:
        """
        Return synthetic log lines for a service.
        If an error_rate anomaly is injected, include ERROR log lines.
        """
        injected = self._injected.get(service, {})
        error_rate = injected.get("error_rate", 0.02)

        logs: List[Log] = []
        now = datetime.utcnow()

        # Generate a realistic mix of log levels
        n_lines = min(max_lines, self._rng.randint(20, 60))

        for i in range(n_lines):
            ts = now - timedelta(seconds=i * 2)

            # Weight error lines by the injected error_rate
            r = self._rng.random()
            if r < error_rate:
                level   = "ERROR"
                message = self._rng.choice(_ERROR_MESSAGES[service] if service in _ERROR_MESSAGES else _ERROR_MESSAGES["default"])
            elif r < error_rate + 0.05:
                level   = "WARN"
                message = self._rng.choice(_WARN_MESSAGES)
            else:
                level   = "INFO"
                message = self._rng.choice(_INFO_MESSAGES)

            logs.append(Log(
                message   = message,
                level     = level,
                service   = service,
                timestamp = ts,
                metadata  = {"line": i},
            ))

        logger.debug("[MockCollector] Collected %d log lines for %s", len(logs), service)
        return logs


# ============================================================
# Fake log message banks (realistic-ish)
# ============================================================

_ERROR_MESSAGES: dict[str, list[str]] = {
    "default": [
        "Unhandled exception in request handler: NullPointerException",
        "Database connection timeout after 30000ms",
        "Failed to deserialize response body: unexpected token",
        "Circuit breaker OPEN — downstream unavailable",
        "Redis connection refused: ECONNREFUSED 127.0.0.1:6379",
        "HTTP 500 Internal Server Error returned to client",
        "Retry limit exceeded (3/3): request aborted",
    ],
    "auth-api": [
        "JWT verification failed: signature mismatch",
        "Token introspection endpoint returned 503",
        "LDAP bind failed: invalid credentials",
        "Session store write failed: timeout",
        "OAuth2 token exchange error: invalid_grant",
    ],
    "payments-api": [
        "Payment gateway timeout after 10000ms",
        "Stripe API returned 402: card_declined",
        "Transaction rollback: deadlock detected",
        "Idempotency key collision: duplicate request",
        "Fraud detection service unreachable",
    ],
}

_WARN_MESSAGES: list[str] = [
    "Response time exceeded SLA threshold (800ms)",
    "Connection pool at 80% capacity",
    "Cache miss rate elevated: 42%",
    "Retrying request (attempt 2/3)",
    "Rate limit approaching: 90% of quota used",
    "Slow query detected: 650ms",
]

_INFO_MESSAGES: list[str] = [
    "Request processed successfully in 95ms",
    "Health check OK",
    "Cache hit: returning cached response",
    "Background job completed: 142 records processed",
    "Deployment heartbeat received",
    "Config refreshed from remote",
    "Connection pool warmed up: 10/10 connections ready",
]