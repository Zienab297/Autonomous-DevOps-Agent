"""
agents/monitoring_agent/file_scanner.py
=========================================
Scans uploaded files/artifacts for malicious, disturbing, or dangerous content.

Two-pass approach:
    Pass 1 — fast keyword/pattern scan (always runs, no I/O)
    Pass 2 — optional Groq LLM deep scan for ambiguous results

Returns a ScanResult that includes:
    - safe: bool
    - risk_level: "clean" | "low" | "medium" | "high" | "critical"
    - findings: list of Finding objects, each with file path, line number,
                matched content, and category
    - summary: human-readable description of what was found

The MonitoringAgent calls this and uses findings to:
    1. Populate the INCIDENT_CREATED / FILE_SCAN_FAILED event payload
    2. Decide whether to trigger a rollback
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ── Risk levels ────────────────────────────────────────────────────────────────

class RiskLevel(str, Enum):
    CLEAN    = "clean"
    LOW      = "low"
    MEDIUM   = "medium"
    HIGH     = "high"
    CRITICAL = "critical"

    def exceeds(self, other: "RiskLevel") -> bool:
        _order = [RiskLevel.CLEAN, RiskLevel.LOW, RiskLevel.MEDIUM,
                  RiskLevel.HIGH, RiskLevel.CRITICAL]
        # FIX Bug #2: use >= so that a risk equal to the threshold is also unsafe
        return _order.index(self) >= _order.index(other)


# ── Finding — one detected problem in one file ─────────────────────────────────

@dataclass
class Finding:
    """
    A single problem found in a scanned file.

    Attributes:
        file_path:   Relative or absolute path of the problematic file
        line_number: Line where the issue was detected (1-indexed, 0 = unknown)
        matched:     The exact content that triggered the finding (truncated)
        category:    Category label e.g. "malware_signature", "secret_leak",
                     "dangerous_command", "disturbing_content", "vulnerability"
        risk_level:  Severity of this individual finding
        detail:      Human-readable explanation
    """
    file_path:   str
    line_number: int
    matched:     str
    category:    str
    risk_level:  RiskLevel
    detail:      str

    def __str__(self) -> str:
        return (
            f"[{self.risk_level.value.upper()}] {self.file_path}:{self.line_number} "
            f"({self.category}) — {self.detail}"
        )

    def to_dict(self) -> dict:
        return {
            "file":       self.file_path,
            "line":       self.line_number,
            "matched":    self.matched[:120],   # never expose huge payloads in events
            "category":   self.category,
            "risk_level": self.risk_level.value,
            "detail":     self.detail,
        }


# ── ScanResult ─────────────────────────────────────────────────────────────────

@dataclass
class ScanResult:
    """
    Aggregated result of scanning one or more files.

    Attributes:
        safe:         True only when risk_level is below the rollback threshold
        risk_level:   Worst risk level across all findings
        findings:     All individual Finding objects (sorted worst-first)
        scanned_files: List of file paths that were scanned
        summary:      One-line human-readable verdict
        llm_used:     Whether the LLM deep-scan pass ran
        scanned_at:   When the scan completed
    """
    safe:          bool
    risk_level:    RiskLevel
    findings:      list[Finding]
    scanned_files: list[str]
    summary:       str
    llm_used:      bool = False
    scanned_at:    datetime = field(default_factory=datetime.utcnow)

    def files_with_problems(self) -> list[dict]:
        """
        Returns findings grouped by file — the format the MonitoringAgent
        attaches to INCIDENT_CREATED / FILE_SCAN_FAILED event payloads.

        Example return value:
            [
              {
                "file": "deploy/start.sh",
                "findings": [
                  {"line": 12, "category": "dangerous_command",
                   "risk_level": "critical", "detail": "...", "matched": "..."},
                ]
              },
              ...
            ]
        """
        grouped: dict[str, list[dict]] = {}
        for f in self.findings:
            grouped.setdefault(f.file_path, []).append({
                "line":       f.line_number,
                "category":   f.category,
                "risk_level": f.risk_level.value,
                "detail":     f.detail,
                "matched":    f.matched[:120],
            })
        return [{"file": fp, "findings": flist} for fp, flist in grouped.items()]

    def __str__(self) -> str:
        return (
            f"ScanResult(safe={self.safe}, risk={self.risk_level.value}, "
            f"findings={len(self.findings)}, files={len(self.scanned_files)})"
        )


# ── Pattern catalogue ──────────────────────────────────────────────────────────
# Each entry: (compiled_regex, category, risk_level, detail_template)

_PATTERNS: list[tuple[re.Pattern, str, RiskLevel, str]] = [

    # ── Secrets / credential leaks ─────────────────────────────────────────
    (re.compile(r'(?i)(password|passwd|pwd)\s*=\s*["\'][^"\']{4,}["\']'),
     "secret_leak", RiskLevel.HIGH, "Hardcoded password literal"),

    (re.compile(r'(?i)(api[_-]?key|apikey|secret[_-]?key)\s*=\s*["\'][^"\']{8,}["\']'),
     "secret_leak", RiskLevel.HIGH, "Hardcoded API/secret key"),

    (re.compile(r'(?i)aws_secret_access_key\s*=\s*[A-Za-z0-9+/]{40}'),
     "secret_leak", RiskLevel.CRITICAL, "AWS secret access key exposed"),

    (re.compile(r'(?i)-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----'),
     "secret_leak", RiskLevel.CRITICAL, "Private key material in file"),

    (re.compile(r'(?i)(token|auth_token|bearer)\s*=\s*["\'][A-Za-z0-9\-_.]{20,}["\']'),
     "secret_leak", RiskLevel.HIGH, "Hardcoded auth token"),

    # ── Malware / shell injection signatures ──────────────────────────────
    (re.compile(r'(?i)(curl|wget)\s+.*\|\s*(ba)?sh'),
     "malware_signature", RiskLevel.CRITICAL, "Remote code execution via pipe to shell"),

    (re.compile(r'(?i)base64\s*(-d|--decode)\s*.*\|\s*(ba)?sh'),
     "malware_signature", RiskLevel.CRITICAL, "Base64-decoded payload piped to shell"),

    (re.compile(r'(?i)eval\s*\(\s*(base64|atob|decode)'),
     "malware_signature", RiskLevel.CRITICAL, "eval() on encoded payload — likely obfuscated code"),

    (re.compile(r'(?i)(import|require)\s*\(\s*["\']child_process["\']'),
     "dangerous_command", RiskLevel.MEDIUM, "Node.js child_process import — review usage"),

    (re.compile(r'(?i)subprocess\.(call|run|Popen)\s*\(.*shell\s*=\s*True'),
     "dangerous_command", RiskLevel.HIGH, "Python subprocess with shell=True — injection risk"),

    # ── Dangerous shell commands ───────────────────────────────────────────
    (re.compile(r'(?i)rm\s+-rf?\s+/(?!tmp|var/tmp)'),
     "dangerous_command", RiskLevel.CRITICAL, "Destructive rm -rf targeting non-tmp path"),

    (re.compile(r'(?i)chmod\s+[0-7]*7[0-7]{2}\s+'),
     "dangerous_command", RiskLevel.MEDIUM, "World-writable chmod — permission escalation risk"),

    (re.compile(r'(?i)(mkfs|dd\s+if=/dev/zero|shred)\s+'),
     "dangerous_command", RiskLevel.CRITICAL, "Disk-wiping command detected"),

    (re.compile(r'(?i):\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;'),
     "malware_signature", RiskLevel.CRITICAL, "Fork bomb pattern detected"),

    # ── Network exfiltration patterns ─────────────────────────────────────
    (re.compile(r'(?i)(nc|netcat|ncat)\s+(-[a-z]+\s+)*\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}'),
     "malware_signature", RiskLevel.HIGH, "Netcat connection to external IP"),

    (re.compile(r'(?i)/dev/(tcp|udp)/[^\s]+/\d+'),
     "malware_signature", RiskLevel.HIGH, "Bash /dev/tcp reverse shell pattern"),

    # ── Supply-chain / dependency tampering ───────────────────────────────
    (re.compile(r'(?i)(pip|npm|gem|cargo)\s+install\s+.*--index-url\s+http://'),
     "vulnerability", RiskLevel.HIGH, "Package install from plain HTTP — MITM risk"),

    (re.compile(r'(?i)__import__\s*\(\s*["\']os["\']'),
     "malware_signature", RiskLevel.HIGH, "Obfuscated os module import"),

    # ── Disturbing / prohibited content markers ───────────────────────────
    (re.compile(r'(?i)(child.{0,10}(abuse|exploit|porn|nude)|csam)'),
     "disturbing_content", RiskLevel.CRITICAL, "Prohibited content marker — CSAM reference"),

    (re.compile(r'(?i)(kill\s+all\s+(user|employee|human)|mass\s+casualt)'),
     "disturbing_content", RiskLevel.CRITICAL, "Violent/threatening language in artifact"),

    # ── Kubernetes-specific risks ─────────────────────────────────────────
    (re.compile(r'(?i)privileged\s*:\s*true'),
     "vulnerability", RiskLevel.HIGH, "Kubernetes privileged container — full host access"),

    (re.compile(r'(?i)hostPID\s*:\s*true|hostNetwork\s*:\s*true'),
     "vulnerability", RiskLevel.HIGH, "Kubernetes host namespace sharing enabled"),

    (re.compile(r'(?i)automountServiceAccountToken\s*:\s*true'),
     "vulnerability", RiskLevel.MEDIUM, "K8s service account token auto-mounted"),
]

# File extensions to scan (binary files skipped)
_TEXT_EXTENSIONS = {
    ".py", ".sh", ".bash", ".js", ".ts", ".yaml", ".yml",
    ".json", ".env", ".cfg", ".conf", ".ini", ".toml",
    ".dockerfile", ".tf", ".hcl", ".rb", ".go", ".java",
    ".php", ".pl", ".ps1", ".psm1", ".bat", ".cmd",
}

# Max file size to scan (skip huge binaries)
_MAX_FILE_BYTES = 5 * 1024 * 1024  # 5 MB

# Risk levels considered "ambiguous" — eligible for LLM deep scan
_LLM_ELIGIBLE_LEVELS = {RiskLevel.LOW, RiskLevel.MEDIUM}


# ── FileScanner ────────────────────────────────────────────────────────────────

class FileScanner:
    """
    Scans files or directories for malicious/disturbing content.

    Usage:
        scanner = FileScanner(rollback_threshold=RiskLevel.HIGH)
        result  = scanner.scan(path="/uploads/artifact.sh")

        if not result.safe:
            for fp in result.files_with_problems():
                print(fp["file"], fp["findings"])
    """

    def __init__(
        self,
        rollback_threshold: RiskLevel = RiskLevel.HIGH,
        llm_analyzer=None,          # optional GroqAnalyzer for deep-scan
    ):
        self.rollback_threshold = rollback_threshold
        self._llm               = llm_analyzer

    def scan(self, path: str | Path) -> ScanResult:
        """
        Scan a file or directory (recursive).
        Returns a ScanResult with all findings and files_with_problems().
        """
        root = Path(path)
        files_to_scan: list[Path] = []

        if root.is_file():
            # FIX Bug #3: apply the same extension + size guard for single files
            if root.suffix.lower() in _TEXT_EXTENSIONS and root.stat().st_size <= _MAX_FILE_BYTES:
                files_to_scan = [root]
            else:
                logger.warning(f"FileScanner: skipping unsupported or oversized file — {path}")
                return self._clean_result([str(path)], [str(path)])
        elif root.is_dir():
            files_to_scan = [
                f for f in root.rglob("*")
                if f.is_file()
                and f.suffix.lower() in _TEXT_EXTENSIONS
                and f.stat().st_size <= _MAX_FILE_BYTES
            ]
        else:
            logger.warning(f"FileScanner: path not found — {path}")
            return self._clean_result([], [str(path)])

        all_findings: list[Finding] = []
        scanned: list[str] = []

        for fpath in files_to_scan:
            findings = self._scan_file(fpath)
            all_findings.extend(findings)
            scanned.append(str(fpath))

        return self._build_result(all_findings, scanned)

    def scan_lines(self, lines: list[str], label: str = "<inline>") -> ScanResult:
        """
        Scan a list of raw strings (e.g. CI/CD log lines) without a real file.
        Used by MonitoringAgent when scanning log content directly.
        """
        findings = self._scan_content(lines, file_path=label)
        return self._build_result(findings, [label])

    # ── Private ────────────────────────────────────────────────────────────────

    def _scan_file(self, path: Path) -> list[Finding]:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception as exc:
            logger.warning(f"FileScanner: could not read {path}: {exc}")
            return []
        return self._scan_content(lines, file_path=str(path))

    def _scan_content(self, lines: list[str], file_path: str) -> list[Finding]:
        findings: list[Finding] = []
        for lineno, line in enumerate(lines, start=1):
            for pattern, category, risk, detail in _PATTERNS:
                m = pattern.search(line)
                if m:
                    findings.append(Finding(
                        file_path   = file_path,
                        line_number = lineno,
                        matched     = m.group(0)[:200],
                        category    = category,
                        risk_level  = risk,
                        detail      = detail,
                    ))
                    break   # one finding per line — don't double-report
        return findings

    def _build_result(self, findings: list[Finding], scanned: list[str]) -> ScanResult:
        if not findings:
            # FIX Bug #1: pass an empty list for the files argument, not scanned twice
            return self._clean_result(scanned, [])

        # Sort worst-first
        _order = [RiskLevel.CLEAN, RiskLevel.LOW, RiskLevel.MEDIUM,
                  RiskLevel.HIGH, RiskLevel.CRITICAL]
        findings.sort(key=lambda f: _order.index(f.risk_level), reverse=True)

        worst = findings[0].risk_level

        # FIX Bug #4: invoke LLM deep-scan for ambiguous (low/medium) results
        llm_used = False
        if self._llm is not None and worst in _LLM_ELIGIBLE_LEVELS:
            try:
                llm_findings = self._llm.analyze(findings, scanned)
                if llm_findings:
                    findings.extend(llm_findings)
                    findings.sort(key=lambda f: _order.index(f.risk_level), reverse=True)
                    worst = findings[0].risk_level
                llm_used = True
                logger.info("FileScanner: LLM deep-scan pass completed")
            except Exception as exc:
                logger.warning(f"FileScanner: LLM deep-scan failed, skipping — {exc}")

        # FIX Bug #2: exceeds() now uses >= so a risk equal to the threshold is unsafe
        safe  = not worst.exceeds(self.rollback_threshold)

        cats  = list({f.category for f in findings})
        files = list({f.file_path for f in findings})
        summary = (
            f"{len(findings)} finding(s) across {len(files)} file(s): "
            f"{', '.join(cats)} — worst risk: {worst.value}"
        )

        logger.warning(f"FileScanner: {summary}")
        for f in findings[:10]:   # log first 10
            logger.warning(f"  {f}")

        return ScanResult(
            safe          = safe,
            risk_level    = worst,
            findings      = findings,
            scanned_files = scanned,
            summary       = summary,
            llm_used      = llm_used,
        )

    @staticmethod
    def _clean_result(scanned: list[str], files: list[str]) -> ScanResult:
        return ScanResult(
            safe          = True,
            risk_level    = RiskLevel.CLEAN,
            findings      = [],
            scanned_files = scanned,
            summary       = f"No issues found in {len(scanned)} file(s)",
        )