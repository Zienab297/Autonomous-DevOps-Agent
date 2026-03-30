"""
agents/monitoring_agent/groq_analyzer.py
-----------------------------------------
Incident analysis using pluggable LLM provider.
Uses get_llm_provider(agent="monitoring").
Falls back to rule-based analysis if provider unavailable.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, List, Optional

from core.models import Severity
from agents.monitoring_agent.detector import Anomaly

logger = logging.getLogger(__name__)

_SEVERITY_MAP = {
    "low":      Severity.LOW,
    "medium":   Severity.MEDIUM,
    "high":     Severity.HIGH,
    "critical": Severity.CRITICAL,
}

_provider = None  # module-level cache


def _get_provider():
    global _provider
    if _provider is None:
        try:
            from providers.llm.llm_selector import get_llm_provider
            _provider = get_llm_provider(agent="monitoring")
        except Exception as e:
            logger.warning("[GroqAnalyzer] Could not load LLM provider: %s", e)
    return _provider


def _chat(prompt: str) -> str:
    from providers.llm.llm_selector import is_quota_error, handle_quota_error
    global _provider
    provider = _get_provider()
    if not provider:
        raise RuntimeError("No LLM provider available")
    try:
        return provider.chat(messages=[{"role": "user", "content": prompt}]).content
    except Exception as e:
        if is_quota_error(e):
            new_p = handle_quota_error(provider, agent="monitoring")
            if new_p:
                _provider = new_p
                return new_p.chat(messages=[{"role": "user", "content": prompt}]).content
        raise


# ── output dataclass ──────────────────────────────────────────────────────────

@dataclass
class IncidentAnalysis:
    severity     : Severity
    root_cause   : str
    impact       : str
    recommended  : str
    confidence   : float
    report       : str
    files_to_fix : List[dict] = field(default_factory=list)
    model        : str = "llm_provider"
    fallback     : bool = False

    def __str__(self):
        return (
            f"IncidentAnalysis("
            f"severity={self.severity.value}, "
            f"confidence={self.confidence:.0%}, "
            f"files_to_fix={len(self.files_to_fix)}, "
            f"fallback={self.fallback})"
        )


# ── prompt builder ────────────────────────────────────────────────────────────

def _build_prompt(service, anomalies, metrics, logs, now) -> str:
    anomaly_lines = "\n".join(
        f"  - [{a.severity.value.upper()}] {a.metric_name}={a.current_value:.4f} "
        f"(threshold={a.threshold:.4f}): {a.message}"
        for a in anomalies
    )
    metric_lines = "\n".join(f"  - {m.name}: {m.value}{m.unit}" for m in metrics)

    traceback_logs   = [l for l in logs if l.level == "ERROR" and "fix_here" in (l.metadata or {})]
    plain_error_logs = [l for l in logs if l.level == "ERROR" and "fix_here" not in (l.metadata or {})]
    has_tracebacks   = bool(traceback_logs)

    if traceback_logs:
        tb_lines = []
        for i, log in enumerate(traceback_logs[:8], 1):
            m = log.metadata or {}
            tb_lines.append(
                f"  [{i}] {m.get('exception','Exception')} in "
                f"{m.get('file','?')} line {m.get('line','?')} in {m.get('function','?')}()"
            )
            tb_lines.append(f"      Message  : {log.message}")
            tb_lines.append(f"      Fix here : {m.get('fix_here','?')}")
            if m.get("full_traceback"):
                tb_tail = "\n".join(m["full_traceback"].splitlines()[-3:])
                tb_lines.append(f"      Traceback tail:\n{tb_tail}")
            tb_lines.append("")
        tb_section = "\n".join(tb_lines)
    else:
        tb_section = "\n".join(
            f"  - [{l.level}] {l.message}" for l in plain_error_logs[:8]
        ) or "  No ERROR log lines in this batch"

    traceback_instruction = (
        "TRACEBACK ANALYSIS REQUIRED: For each traceback, identify the exact file and line to fix. "
        "Populate the files_to_fix array with ALL unique fix locations."
        if has_tracebacks else ""
    )

    files_to_fix_schema = (
        '  "files_to_fix": [{"file": "deploy.py", "line": 47, "function": "run_pipeline", '
        '"exception": "KeyError: AWS_REGION", "fix_description": "What needs to change"}],'
        if has_tracebacks else '  "files_to_fix": [],'
    )

    return f"""You are an expert SRE analyzing a live production incident.

SERVICE:  {service}
TIME:     {now}

DETECTED ANOMALIES:
{anomaly_lines}

CURRENT METRICS:
{metric_lines}

{"TRACEBACKS FROM CI/CD LOGS (" + str(len(traceback_logs)) + " found):" if has_tracebacks else "RECENT ERROR LOGS:"}
{tb_section}

{traceback_instruction}

Analyze this incident. Respond with ONLY a valid JSON object — no markdown fences, no explanation outside the JSON.

{{
  "severity": "low|medium|high|critical",
  "root_cause": "One sentence — the most likely underlying cause",
  "impact": "One sentence — what users or systems are affected right now",
  "recommended_action": "One sentence — the single best immediate action",
  "confidence": 0.0,
  "incident_report": "4-6 sentence human-readable report.",
  {files_to_fix_schema}
}}

Rules:
- severity must be exactly: low | medium | high | critical
- confidence must be a float 0.0–1.0
- Be specific — reference actual metric values and service name
"""


# ── analyzer ──────────────────────────────────────────────────────────────────

class GroqAnalyzer:

    def __init__(self, api_key: Optional[str] = None, model: str = "", timeout: int = 30):
        # api_key / model kept for backward compat but ignored — we use LLM provider
        pass

    @property
    def available(self) -> bool:
        return _get_provider() is not None

    async def analyze(
        self,
        service   : str,
        anomalies : list,
        metrics   : list,
        logs      : list,
    ) -> IncidentAnalysis:
        if not self.available:
            return self._fallback(anomalies, logs, service)
        try:
            return await self._call_llm(service, anomalies, metrics, logs)
        except Exception as exc:
            logger.error("[GroqAnalyzer] API call failed (%s) — using fallback", exc)
            return self._fallback(anomalies, logs, service)

    async def _call_llm(self, service, anomalies, metrics, logs) -> IncidentAnalysis:
        import asyncio
        prompt = _build_prompt(
            service   = service,
            anomalies = anomalies,
            metrics   = metrics,
            logs      = logs,
            now       = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
        )
        # Run sync LLM call in thread pool to not block event loop
        raw = await asyncio.get_event_loop().run_in_executor(None, lambda: _chat(prompt))
        logger.debug("[GroqAnalyzer] Raw response: %s", raw[:200])
        return self._parse(raw)

    def _parse(self, raw: str) -> IncidentAnalysis:
        """
        Robustly extract and parse the JSON object from the LLM response.

        Handles:
        - Clean JSON with no wrapper
        - JSON wrapped in ```json ... ``` fences
        - JSON buried after explanation text
        - Unescaped quotes / backslashes from log content in the prompt
        """
        # Step 1: extract the JSON object by finding the outermost { ... }
        # This handles cases where the LLM adds explanation before/after
        brace_start = raw.find("{")
        brace_end   = raw.rfind("}")
        if brace_start == -1 or brace_end == -1 or brace_end <= brace_start:
            raise ValueError("No JSON object found in LLM response")

        clean = raw[brace_start : brace_end + 1]

        # Step 2: try direct parse first
        try:
            parsed = json.loads(clean)
        except json.JSONDecodeError:
            # Step 3: repair common LLM JSON mistakes
            import re as _re
            # Remove trailing commas before } or ]
            clean = _re.sub(r",\s*([}\]])", r"\1", clean)
            # Replace smart quotes with straight quotes
            clean = clean.replace("\u201c", '"').replace("\u201d", '"')
            clean = clean.replace("\u2018", "'").replace("\u2019", "'")
            # Fix unescaped Windows backslash paths (e.g. D:\rename\file.py)
            # Only fix lone backslashes that aren't already a valid escape
            clean = _re.sub(r"\\(?![\"'/nrtbfu])", r"\\\\", clean)
            try:
                parsed = json.loads(clean)
            except json.JSONDecodeError:
                # Last resort: extract just the string values we need
                # using regex rather than full JSON parse
                def _extract(key):
                    m = _re.search(
                        r'"' + key + r'"\s*:\s*"((?:[^"\\]|\\.)*)"', clean
                    )
                    return m.group(1) if m else ""
                def _extract_float(key):
                    m = _re.search(r'"' + key + r'"\s*:\s*([0-9.]+)', clean)
                    try:
                        return float(m.group(1)) if m else 0.7
                    except ValueError:
                        return 0.7
                parsed = {
                    "severity"         : _extract("severity") or "medium",
                    "root_cause"       : _extract("root_cause"),
                    "impact"           : _extract("impact"),
                    "recommended_action": _extract("recommended_action"),
                    "confidence"       : _extract_float("confidence"),
                    "incident_report"  : _extract("incident_report"),
                    "files_to_fix"     : [],
                }
        severity   = _SEVERITY_MAP.get(parsed.get("severity","medium").lower(), Severity.MEDIUM)
        confidence = float(parsed.get("confidence", 0.7))
        confidence = max(0.0, min(1.0, confidence))

        raw_files    = parsed.get("files_to_fix", [])
        files_to_fix = []
        for entry in raw_files:
            if isinstance(entry, dict) and entry.get("file"):
                files_to_fix.append({
                    "file"            : entry.get("file", ""),
                    "line"            : int(entry.get("line", 0)),
                    "function"        : entry.get("function", ""),
                    "exception"       : entry.get("exception", ""),
                    "fix_description" : entry.get("fix_description", ""),
                })

        return IncidentAnalysis(
            severity     = severity,
            root_cause   = parsed.get("root_cause",        "Unknown root cause"),
            impact       = parsed.get("impact",            "Impact unknown"),
            recommended  = parsed.get("recommended_action","Manual investigation required"),
            confidence   = confidence,
            report       = parsed.get("incident_report",   raw),
            files_to_fix = files_to_fix,
            model        = getattr(_get_provider(), "name", "unknown"),
            fallback     = False,
        )

    def _fallback(self, anomalies, logs, service) -> IncidentAnalysis:
        from agents.monitoring_agent.incident_factory import _SEVERITY_ORDER
        worst = max(anomalies, key=lambda a: _SEVERITY_ORDER.index(a.severity))
        sev   = worst.severity
        msg   = worst.message

        files_to_fix = []
        seen = set()
        for log in logs:
            if log.level == "ERROR" and log.metadata and "fix_here" in log.metadata:
                key = log.metadata["fix_here"]
                if key not in seen:
                    seen.add(key)
                    files_to_fix.append({
                        "file"            : log.metadata.get("file", ""),
                        "line"            : log.metadata.get("line", 0),
                        "function"        : log.metadata.get("function", ""),
                        "exception"       : log.metadata.get("exception", ""),
                        "fix_description" : f"Exception raised at {key}",
                    })

        fix_summary = (
            " Files to fix: " + ", ".join(
                f"{f['file']}:{f['line']}" for f in files_to_fix[:5]
            ) + "."
            if files_to_fix else ""
        )

        return IncidentAnalysis(
            severity     = sev,
            root_cause   = msg,
            impact       = f"{service} is degraded — users may be affected",
            recommended  = "Investigate recent deployments and check downstream dependencies." + fix_summary,
            confidence   = 0.6,
            report       = (
                f"{service} is experiencing an incident. {msg}.{fix_summary} "
                f"Severity: {sev.value}. "
                f"This assessment was generated by rule-based fallback (LLM unavailable)."
            ),
            files_to_fix = files_to_fix,
            model        = "fallback",
            fallback     = True,
        )