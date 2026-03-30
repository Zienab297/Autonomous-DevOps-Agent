"""
agents/monitoring_agent/file_collector.py
------------------------------------------
A real BaseCollector implementation that reads CI/CD log files from disk
and extracts structured Metric + Log objects for the MonitoringAgent.

This replaces MockCollector when collector_backend = "file" in config.

How it works
------------
1. On every poll, FileCollector scans the log directory for each service.
2. It reads only NEW bytes since the last poll (incremental — no duplicates).
3. LogParser extracts tracebacks: file, line, function, exception type.
4. The collector builds:
      Metric("error_rate")        — fraction of log lines that are tracebacks
      Metric("traceback_count")   — raw number of tracebacks found
   And for each traceback, a Log object whose metadata carries:
      {"file": "deploy.py", "line": 47, "function": "run_pipeline",
       "exception": "KeyError", "fix_here": "deploy.py:47"}

5. The Detector sees the error_rate metric and fires anomalies.
6. The IncidentFactory + GroqAnalyzer build a rich incident.
7. The INCIDENT_CREATED event data includes the exact files to fix.

Directory layout
----------------
logs/
  auth-api/
    run_2024-03-24_02-13.log     ← one file per CI/CD run
    run_2024-03-24_08-45.log
  payments-api/
    run_2024-03-24_03-00.log
  inventory-service/
    run_2024-03-24_04-00.log

Each subfolder name is used as the service name.
If you use a flat layout (all logs in one directory), the service name
falls back to the log filename stem.

Usage in config.py
------------------
    config = MonitoringConfig(
        services=["auth-api", "payments-api"],
        collector_backend="file",
        log_dir="logs",           # add this field to MonitoringConfig
    )

Usage in agent._build_collector()
----------------------------------
    if backend == "file":
        return FileCollector(log_dir=self._config.log_dir)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from core.models import Log, Metric
from agents.monitoring_agent.collector import BaseCollector
from agents.monitoring_agent.log_parser import LogParser, ParseResult

logger = logging.getLogger(__name__)


class FileCollector(BaseCollector):
    """
    Reads CI/CD log files from disk and converts them into Metric + Log objects.

    Key feature: tracks the byte offset of every file it has read so that
    on each subsequent poll it only processes NEW content — no duplicate
    incidents from the same traceback.

    Example:
        collector = FileCollector(log_dir="logs")
        metrics = await collector.collect_metrics("auth-api")
        logs    = await collector.collect_logs("auth-api")
    """

    def __init__(
        self,
        log_dir       : str = "logs",
        log_pattern   : str = "*.log",
    ):
        self._log_dir     = Path(log_dir)
        self._log_pattern = log_pattern
        self._parser      = LogParser()

        # Track how far into each file we've already read.
        # key: absolute file path string → bytes consumed
        self._file_offsets: Dict[str, int] = {}

        logger.info(
            "[FileCollector] Initialized — log_dir=%s, pattern=%s",
            self._log_dir, self._log_pattern,
        )

    # ── BaseCollector interface ───────────────────────────────────────────────

    async def collect_metrics(self, service: str) -> List[Metric]:
        """
        Parse new log content for the service and return error-rate metrics.

        Returns two metrics:
            error_rate      — fraction of lines that are traceback lines
            traceback_count — raw number of distinct tracebacks found
        """
        results = self._scan_service(service)
        now = datetime.now(timezone.utc)

        total_errors = sum(len(r.errors) for r in results)
        error_rate   = (
            sum(r.error_rate for r in results) / len(results)
            if results else 0.0
        )

        metrics = [
            Metric(
                name      = "error_rate",
                value     = round(error_rate, 4),
                unit      = "%",
                service   = service,
                timestamp = now,
                labels    = {"source": "log_file", "env": "ci"},
            ),
            Metric(
                name      = "traceback_count",
                value     = float(total_errors),
                unit      = "count",
                service   = service,
                timestamp = now,
                labels    = {"source": "log_file", "env": "ci"},
            ),
        ]

        logger.debug(
            "[FileCollector] %s metrics — error_rate=%.4f tracebacks=%d",
            service, error_rate, total_errors,
        )
        return metrics

    async def collect_logs(self, service: str, max_lines: int = 100) -> List[Log]:
        """
        Return Log objects for each traceback found in the service's log files.

        Each Log object carries the traceback details in its metadata:
            metadata["file"]       = "deploy.py"
            metadata["line"]       = 47
            metadata["function"]   = "run_pipeline"
            metadata["exception"]  = "KeyError"
            metadata["fix_here"]   = "deploy.py:47"
            metadata["log_file"]   = "logs/auth-api/run_2024-03-24.log"

        This metadata flows all the way to the INCIDENT_CREATED event so
        downstream agents (and you) know exactly which files to fix.
        """
        results = self._scan_service(service)
        logs: List[Log] = []

        for parse_result in results:
            for err in parse_result.errors:
                log = Log(
                    message   = err.message,
                    level     = "ERROR",
                    service   = service,
                    timestamp = err.timestamp or datetime.now(timezone.utc),
                    metadata  = {
                        # PRIMARY — the exact location to fix
                        "fix_here"      : f"{err.file}:{err.line}",
                        "file"          : err.file,
                        "line"          : err.line,
                        "function"      : err.function,
                        # Context
                        "exception"     : err.exception_type,
                        "log_file"      : err.log_file,
                        "full_traceback": err.full_traceback,
                    },
                )
                logs.append(log)
                if len(logs) >= max_lines:
                    break
            if len(logs) >= max_lines:
                break

        logger.debug(
            "[FileCollector] %s → %d traceback log entries", service, len(logs)
        )
        return logs

    async def health_check(self) -> bool:
        """Verify the log directory exists and is readable."""
        if not self._log_dir.is_dir():
            logger.warning("[FileCollector] Log directory not found: %s", self._log_dir)
            return False
        return True

    # ── helpers ───────────────────────────────────────────────────────────────

    def _scan_service(self, service: str) -> List[ParseResult]:
        """
        Find and incrementally parse all log files for a service.

        Looks in: {log_dir}/{service}/*.log
        Falls back to: {log_dir}/*.log (filtered by service name in stem)
        """
        service_dir = self._log_dir / service

        if service_dir.is_dir():
            files = sorted(service_dir.glob(self._log_pattern))
        else:
            # Flat layout: filter files whose stem contains the service name
            all_files = sorted(self._log_dir.glob(self._log_pattern))
            files = [f for f in all_files if service in f.stem]

        if not files:
            logger.debug("[FileCollector] No log files found for service: %s", service)
            return []

        results = []
        for f in files:
            key    = str(f.resolve())
            offset = self._file_offsets.get(key, 0)
            result = self._parser.parse_since(f, byte_offset=offset)

            # Update offset so next poll only reads new content
            self._file_offsets[key] = result.byte_offset

            if result.errors:
                results.append(result)
                logger.info(
                    "[FileCollector] %s — %d new traceback(s) in %s",
                    service, len(result.errors), f.name,
                )

        return results

    def reset_offsets(self, service: Optional[str] = None) -> None:
        """
        Force a full re-read of all files on the next poll.
        Pass service=None to reset everything, or a service name to reset
        only that service's files.
        """
        if service is None:
            self._file_offsets.clear()
            logger.info("[FileCollector] All file offsets reset")
        else:
            service_dir = self._log_dir / service
            keys_to_clear = [
                k for k in self._file_offsets
                if str(service_dir) in k
            ]
            for k in keys_to_clear:
                del self._file_offsets[k]
            logger.info("[FileCollector] Offsets reset for service: %s", service)