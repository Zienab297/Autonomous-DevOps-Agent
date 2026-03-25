"""
agents/monitoring_agent/log_parser.py
---------------------------------------
Parses CI/CD log files and extracts structured error information.

Primary goal: given a log file (or directory of log files), find every
Python traceback and return the exact file + line number that failed —
so the developer can go directly to the broken code and fix it.

Output model
------------
ParsedError
    log_file       : which log file this came from
    service        : derived from the parent directory name
    exception_type : e.g. "KeyError", "ConnectionError"
    message        : e.g. "KeyError: 'AWS_REGION'"
    file           : e.g. "deploy.py"
    line           : e.g. 47
    function       : e.g. "run_pipeline"
    full_traceback : the raw multi-line traceback string
    timestamp      : parsed from the log line before the traceback, if present
    level          : "ERROR" | "WARNING" | "INFO" | "UNKNOWN"

ParseResult (one per log file)
    service        : service name
    log_file       : absolute path to the file that was parsed
    scanned_at     : when the parse happened
    errors         : List[ParsedError]
    raw_log_lines  : total lines in the file
    error_rate     : len(errors) / total_lines (used by Detector)

LogParser
    parse_file(path)              → ParseResult
    parse_directory(path)         → List[ParseResult]
    parse_since(path, byte_offset)→ ParseResult  ← for incremental polling

Usage (standalone Layer 1)
--------------------------
    from agents.monitoring_agent.log_parser import LogParser

    parser = LogParser()
    result = parser.parse_file("logs/auth-api/run_2024-03-24.log")

    for err in result.errors:
        print(f"  {err.file}:{err.line}  {err.exception_type}: {err.message}")

Usage (Layer 2 — fed into FileCollector → MonitoringAgent)
----------------------------------------------------------
    # FileCollector calls parser.parse_since() on each poll so only
    # new lines are processed. See file_collector.py.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── regex patterns ────────────────────────────────────────────────────────────

# Matches: "  File "deploy.py", line 47, in run_pipeline"
_FRAME_RE = re.compile(
    r'^\s*File "(?P<file>[^"]+)",\s+line (?P<line>\d+),\s+in (?P<func>\S+)'
)

# Matches the exception line at the end of a traceback:
#   "KeyError: 'AWS_REGION'"
#   "sqlalchemy.exc.OperationalError: (psycopg2.OperationalError) could not ..."
_EXCEPTION_RE = re.compile(
    r'^(?P<exc_type>[\w.]+(?:Error|Exception|Warning|Fault|Interrupt|Stop'
    r'|KeyboardInterrupt|SystemExit|GeneratorExit|Timeout|Failure|Refused'
    r'|Denied|Abort|Invalid|Missing|Overflow|Underflow|Broken|Lost|Dead'
    r'|Unreachable|Unavailable|Cancelled|Expired|Stale|Corrupt))'
    r'(?P<msg>.*)',
    re.IGNORECASE,
)

# Matches common log line prefixes to extract timestamp + level:
#   "2024-03-24 02:13:50 ERROR ..."
#   "2024-03-24T02:13:50.123Z [ERROR] ..."
#   "[2024-03-24 02:13:50] ERROR ..."
_LOG_PREFIX_RE = re.compile(
    r'(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)'
    r'\s+(?:\[)?(?P<level>ERROR|WARNING|WARN|INFO|DEBUG|CRITICAL|FATAL)(?:\])?\s*'
)


# ── data models ───────────────────────────────────────────────────────────────

@dataclass
class ParsedError:
    """
    One extracted traceback with the precise location to fix.

    The most actionable fields for a developer:
        file + line  → go here to fix it
        exception_type + message → what went wrong
        full_traceback → complete context
    """
    log_file       : str             # e.g. "logs/auth-api/run_2024-03-24.log"
    service        : str             # e.g. "auth-api"
    exception_type : str             # e.g. "KeyError"
    message        : str             # e.g. "KeyError: 'AWS_REGION'"
    file           : str             # e.g. "deploy.py"
    line           : int             # e.g. 47
    function       : str             # e.g. "run_pipeline"
    full_traceback : str             # raw multi-line traceback text
    timestamp      : Optional[datetime] = None
    level          : str = "ERROR"

    def __str__(self) -> str:
        return (
            f"[{self.service}] {self.file}:{self.line} "
            f"in {self.function}() — {self.message}"
        )

    def to_dict(self) -> dict:
        return {
            "log_file"      : self.log_file,
            "service"       : self.service,
            "exception_type": self.exception_type,
            "message"       : self.message,
            "file"          : self.file,
            "line"          : self.line,
            "function"      : self.function,
            "full_traceback": self.full_traceback,
            "timestamp"     : self.timestamp.isoformat() if self.timestamp else None,
            "level"         : self.level,
        }


@dataclass
class ParseResult:
    """
    Everything extracted from a single log file.
    Used by FileCollector to build Metric + Log objects.
    """
    service       : str
    log_file      : str                   # absolute path
    scanned_at    : datetime = field(default_factory=datetime.utcnow)
    errors        : List[ParsedError] = field(default_factory=list)
    raw_log_lines : int = 0
    byte_offset   : int = 0               # bytes consumed — for incremental reads

    @property
    def error_rate(self) -> float:
        """Fraction of log lines that belong to error tracebacks."""
        if self.raw_log_lines == 0:
            return 0.0
        # Count all lines that are part of any traceback (≥3 lines each)
        traceback_lines = sum(
            len(e.full_traceback.splitlines()) for e in self.errors
        )
        return min(1.0, traceback_lines / self.raw_log_lines)

    @property
    def traceback_count(self) -> int:
        return len(self.errors)

    def to_dict(self) -> dict:
        return {
            "service"      : self.service,
            "log_file"     : self.log_file,
            "scanned_at"   : self.scanned_at.isoformat(),
            "error_count"  : len(self.errors),
            "error_rate"   : round(self.error_rate, 4),
            "raw_log_lines": self.raw_log_lines,
            "errors"       : [e.to_dict() for e in self.errors],
        }


# ── parser ────────────────────────────────────────────────────────────────────

class LogParser:
    """
    Extracts tracebacks from log files and returns structured ParseResult objects.

    Handles:
    - Python tracebacks (Traceback (most recent call last):)
    - Multi-exception chains (chained with "During handling of...", "The above exception...")
    - Log lines with timestamps before the traceback header
    - Incremental reads (parse_since) for the polling loop

    Does NOT handle:
    - Java stack traces (different format — add _parse_java_traceback() later)
    - Go panic output (add _parse_go_panic() later)
    - Node.js stack traces (add _parse_node_traceback() later)
    """

    def parse_file(self, path: str | Path) -> ParseResult:
        """
        Parse an entire log file and return all extracted errors.

        Args:
            path: Path to the log file

        Returns:
            ParseResult with all tracebacks found in the file
        """
        path = Path(path)
        service = self._service_from_path(path)

        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            logger.error("[LogParser] Cannot read %s: %s", path, e)
            return ParseResult(service=service, log_file=str(path))

        result = self._parse_text(text, service=service, log_file=str(path))
        result.byte_offset = path.stat().st_size
        logger.debug(
            "[LogParser] %s → %d errors in %d lines",
            path.name, len(result.errors), result.raw_log_lines,
        )
        return result

    def parse_since(self, path: str | Path, byte_offset: int = 0) -> ParseResult:
        """
        Parse only the new content appended to a log file since the last read.
        Used by FileCollector for incremental polling.

        Args:
            path:        Path to the log file
            byte_offset: Byte position to start reading from (0 = full file)

        Returns:
            ParseResult containing only errors found in the new content.
            result.byte_offset is updated to the new end-of-file position.
        """
        path = Path(path)
        service = self._service_from_path(path)

        try:
            stat = path.stat()
        except OSError:
            return ParseResult(service=service, log_file=str(path))

        if stat.st_size <= byte_offset:
            # No new content
            return ParseResult(
                service=service,
                log_file=str(path),
                byte_offset=byte_offset,
            )

        try:
            with open(path, "rb") as f:
                f.seek(byte_offset)
                new_bytes = f.read()
            new_text = new_bytes.decode("utf-8", errors="replace")
        except OSError as e:
            logger.error("[LogParser] Cannot read %s at offset %d: %s", path, byte_offset, e)
            return ParseResult(service=service, log_file=str(path), byte_offset=byte_offset)

        result = self._parse_text(new_text, service=service, log_file=str(path))
        result.byte_offset = stat.st_size
        return result

    def parse_directory(
        self,
        directory: str | Path,
        pattern: str = "*.log",
        recursive: bool = True,
    ) -> List[ParseResult]:
        """
        Parse all matching log files in a directory.

        Directory layout expected:
            logs/
              auth-api/           ← service name inferred from folder
                run_2024-03-24_02-13.log
                run_2024-03-24_08-45.log
              payments-api/
                run_2024-03-24_03-00.log

        Args:
            directory: Root directory to scan
            pattern:   Glob pattern for log files (default: "*.log")
            recursive: Whether to recurse into subdirectories

        Returns:
            List of ParseResult, one per log file found
        """
        directory = Path(directory)
        if not directory.is_dir():
            logger.warning("[LogParser] Directory not found: %s", directory)
            return []

        glob = "**/" + pattern if recursive else pattern
        files = sorted(directory.glob(glob))

        results = []
        for f in files:
            if f.is_file():
                results.append(self.parse_file(f))

        logger.info(
            "[LogParser] Scanned %d files in %s — total errors: %d",
            len(files), directory,
            sum(len(r.errors) for r in results),
        )
        return results

    # ── internal ──────────────────────────────────────────────────────────────

    def _parse_text(
        self,
        text: str,
        service: str,
        log_file: str,
    ) -> ParseResult:
        """Core parser: scan lines, find tracebacks, build ParsedError objects."""
        lines = text.splitlines()
        errors: List[ParsedError] = []

        i = 0
        current_ts: Optional[datetime] = None
        current_level: str = "ERROR"

        while i < len(lines):
            line = lines[i]

            # Track the most recent timestamp/level seen before a traceback
            prefix = _LOG_PREFIX_RE.search(line)
            if prefix:
                current_ts    = self._parse_ts(prefix.group("ts"))
                current_level = prefix.group("level").upper()
                if current_level == "WARN":
                    current_level = "WARNING"

            # Detect start of a Python traceback
            if "Traceback (most recent call last)" in line:
                traceback_lines, exc_type, exc_msg, frames = self._collect_traceback(lines, i)

                if frames:
                    # Use the LAST (innermost) frame as the primary fix location
                    last_frame = frames[-1]
                    errors.append(ParsedError(
                        log_file       = log_file,
                        service        = service,
                        exception_type = exc_type,
                        message        = exc_msg,
                        file           = last_frame["file"],
                        line           = last_frame["line"],
                        function       = last_frame["func"],
                        full_traceback = "\n".join(traceback_lines),
                        timestamp      = current_ts,
                        level          = current_level,
                    ))

                i += len(traceback_lines)
                continue

            i += 1

        return ParseResult(
            service       = service,
            log_file      = log_file,
            errors        = errors,
            raw_log_lines = len(lines),
        )

    def _collect_traceback(
        self,
        lines: list[str],
        start: int,
    ) -> Tuple[list[str], str, str, list[dict]]:
        """
        Starting at lines[start] ("Traceback (most recent call last):"),
        collect all lines until the exception line at the end.

        Returns:
            (traceback_lines, exception_type, exception_message, frames)
        """
        tb_lines = [lines[start]]
        frames: list[dict] = []
        exc_type = "UnknownException"
        exc_msg  = ""

        i = start + 1
        while i < len(lines):
            raw = lines[i]
            stripped = raw.strip()

            # New log-level line that isn't part of this traceback
            if (
                stripped == ""
                and i > start + 2
                and i + 1 < len(lines)
                and _LOG_PREFIX_RE.match(lines[i + 1])
            ):
                break

            # Frame line: "  File "x.py", line N, in func"
            frame_match = _FRAME_RE.match(raw)
            if frame_match:
                frames.append({
                    "file": frame_match.group("file"),
                    "line": int(frame_match.group("line")),
                    "func": frame_match.group("func"),
                })
                tb_lines.append(raw)
                i += 1
                continue

            # Code snippet line inside a frame (indented, not a frame line itself)
            if raw.startswith("    ") and not stripped.startswith("File "):
                tb_lines.append(raw)
                i += 1
                continue

            # Exception line (e.g. "KeyError: 'AWS_REGION'")
            exc_match = _EXCEPTION_RE.match(stripped)
            if exc_match:
                exc_type = exc_match.group("exc_type").split(".")[-1]  # short name
                exc_msg  = stripped
                tb_lines.append(raw)
                i += 1
                break

            # Exception chaining banners
            if stripped.startswith("During handling") or stripped.startswith("The above"):
                tb_lines.append(raw)
                i += 1
                continue

            # Line that clearly ends the traceback (new timestamp, empty line, etc.)
            if _LOG_PREFIX_RE.match(raw) or (stripped == "" and i > start + 2):
                break

            tb_lines.append(raw)
            i += 1

        return tb_lines, exc_type, exc_msg, frames

    @staticmethod
    def _service_from_path(path: Path) -> str:
        """
        Infer service name from the file path.

        logs/auth-api/run_2024-03-24.log  → "auth-api"
        logs/payments-api.log             → "payments-api"
        /var/log/deploy.log               → "deploy"
        """
        # If parent directory name looks like a service (not "logs", ".", "/")
        parent = path.parent.name
        generic = {"logs", "log", ".", "", "/"}
        if parent and parent.lower() not in generic:
            return parent
        # Fall back to stem of the filename
        return path.stem

    @staticmethod
    def _parse_ts(ts_str: str) -> Optional[datetime]:
        """Try a few common timestamp formats."""
        formats = [
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%d %H:%M:%S.%f",
        ]
        ts_clean = ts_str.rstrip("Z").split("+")[0].split("-")[0] if "T" in ts_str else ts_str
        # Try the cleaned version first, then the original
        for fmt in formats:
            for candidate in (ts_str.rstrip("Z"), ts_str):
                try:
                    return datetime.strptime(candidate[:19], fmt[:len(fmt)])
                except ValueError:
                    continue
        return None


# ── standalone CLI report ─────────────────────────────────────────────────────

def print_error_report(results: List[ParseResult]) -> None:
    """
    Print a human-readable error report to stdout.
    Tells the developer exactly which files and lines to fix.

    Output example:
        ══════════════════════════════════════════════
        ERRORS FOUND — 2024-03-24 02:14:11 UTC
        ══════════════════════════════════════════════

        [1] auth-api  ←  logs/auth-api/run_2024-03-24.log
            Fix     : deploy.py  line 47  in run_pipeline()
            Error   : KeyError: 'AWS_REGION'
            Traceback:
              File "deploy.py", line 47, in run_pipeline
                client = boto3.client(...)
              KeyError: 'AWS_REGION'
    """
    total_errors = sum(len(r.errors) for r in results)
    if total_errors == 0:
        print("\n✓ No errors found in any log files.\n")
        return

    print("\n" + "═" * 56)
    print(f"  ERRORS FOUND — {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("═" * 56 + "\n")

    idx = 1
    for result in results:
        if not result.errors:
            continue
        for err in result.errors:
            print(f"[{idx}]  Service   : {err.service}")
            print(f"     Log file  : {err.log_file}")
            print(f"     Fix here  : {err.file}  line {err.line}  in {err.function}()")
            print(f"     Error     : {err.message}")
            if err.timestamp:
                print(f"     Time      : {err.timestamp.strftime('%Y-%m-%d %H:%M:%S')}")
            print(f"     Traceback :")
            for tb_line in err.full_traceback.splitlines():
                print(f"       {tb_line}")
            print()
            idx += 1

    print(f"  Total: {total_errors} error(s) across {len(results)} file(s)\n")