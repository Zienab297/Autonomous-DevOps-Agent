"""
agents/monitoring_agent/groq_analyzer.py
-----------------------------------------
Sends detected anomalies to Groq (llama-3.3-70b-versatile) and returns a
structured IncidentAnalysis containing:

    - severity       : re-assessed from all anomaly signals together
    - root_cause     : one sentence — the most likely underlying cause
    - impact         : one sentence — what users / systems are affected
    - recommended    : one sentence — the best immediate action
    - confidence     : 0.0 – 1.0 float
    - report         : 4-6 sentence human-readable incident report
    - files_to_fix   : list of {file, line, function, exception} dicts
                       — present when CI/CD log tracebacks are available

Called by MonitoringAgent._poll_service() AFTER anomalies are detected
and AFTER the IncidentFactory creates the base Incident, but BEFORE the
INCIDENT_CREATED event is published. The Incident's severity and
description are replaced with the LLM output so downstream agents
(KnowledgeAgent, AlertingAgent) receive rich context immediately.

Fallback behaviour
------------------
If GROQ_API_KEY is missing or the API call fails for any reason,
GroqAnalyzer.analyze() returns a FallbackAnalysis built from the
Detector's rule-based output — no exception is raised, the agent
continues normally.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, List, Optional

import aiohttp

from core.models import Severity
from agents.monitoring_agent.detector import Anomaly

logger = logging.getLogger(__name__)

GROQ_URL   = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.3-70b-versatile"

_SEVERITY_MAP = {
    "low":      Severity.LOW,
    "medium":   Severity.MEDIUM,
    "high":     Severity.HIGH,
    "critical": Severity.CRITICAL,
}


# ── output dataclass ──────────────────────────────────────────────────────────

@dataclass
class IncidentAnalysis:
    """
    Structured output from the Groq LLM.
    Replaces the rule-based severity and description on the Incident.
    Stored in Incident.metadata["llm_analysis"] for downstream agents.

    New field: files_to_fix
        When the FileCollector is in use, the LLM extracts a prioritised
        list of source files to fix from the traceback evidence in the logs.
        Each entry: {"file": "deploy.py", "line": 47, "function": "run_pipeline",
                     "exception": "KeyError: 'AWS_REGION'"}
    """
    severity     : Severity
    root_cause   : str
    impact       : str
    recommended  : str
    confidence   : float
    report       : str
    files_to_fix : List[dict] = field(default_factory=list)
    model        : str = GROQ_MODEL
    fallback     : bool = False   # True when rule-based fallback was used

    def __str__(self):
        return (
            f"IncidentAnalysis("
            f"severity={self.severity.value}, "
            f"confidence={self.confidence:.0%}, "
            f"files_to_fix={len(self.files_to_fix)}, "
            f"fallback={self.fallback})"
        )


# ── prompt builder ────────────────────────────────────────────────────────────

def _build_prompt(
    service:   str,
    anomalies: list[Anomaly],
    metrics:   list[Any],
    logs:      list[Any],
    now:       str,
) -> str:
    anomaly_lines = "\n".join(
        f"  - [{a.severity.value.upper()}] {a.metric_name}={a.current_value:.4f} "
        f"(threshold={a.threshold:.4f}): {a.message}"
        for a in anomalies
    )

    metric_lines = "\n".join(
        f"  - {m.name}: {m.value}{m.unit}"
        for m in metrics
    )

    # Split logs into traceback-enriched (from FileCollector) and plain ERROR logs
    traceback_logs = [l for l in logs if l.level == "ERROR" and "fix_here" in (l.metadata or {})]
    plain_error_logs = [l for l in logs if l.level == "ERROR" and "fix_here" not in (l.metadata or {})]

    has_tracebacks = bool(traceback_logs)

    # Build traceback section — this is the primary "files to fix" signal
    if traceback_logs:
        tb_section_lines = []
        for i, log in enumerate(traceback_logs[:8], 1):
            m = log.metadata or {}
            tb_section_lines.append(
                f"  [{i}] {m.get('exception', 'Exception')} in "
                f"{m.get('file', '?')} line {m.get('line', '?')} "
                f"in {m.get('function', '?')}()"
            )
            tb_section_lines.append(f"      Message  : {log.message}")
            tb_section_lines.append(f"      Fix here : {m.get('fix_here', '?')}")
            if m.get("full_traceback"):
                # Include just the last 3 lines of the traceback for context
                tb_tail = "\n".join(m["full_traceback"].splitlines()[-3:])
                tb_section_lines.append(f"      Traceback tail:\n{tb_tail}")
            tb_section_lines.append("")
        tb_section = "\n".join(tb_section_lines)
    else:
        error_logs = plain_error_logs[:8]
        tb_section = "\n".join(
            f"  - [{l.level}] {l.message}" for l in error_logs
        ) or "  No ERROR log lines in this batch"

    traceback_instruction = (
        """
TRACEBACK ANALYSIS REQUIRED:
The logs above contain real Python tracebacks from a CI/CD pipeline failure.
For each traceback, identify the exact file and line to fix.
Populate the "files_to_fix" array in your response with ALL unique fix locations.
Order them by priority (most likely root cause first).
"""
        if has_tracebacks
        else ""
    )

    files_to_fix_schema = (
        """
  "files_to_fix": [
    {
      "file": "deploy.py",
      "line": 47,
      "function": "run_pipeline",
      "exception": "KeyError: 'AWS_REGION'",
      "fix_description": "One sentence — what needs to change in this file"
    }
  ],"""
        if has_tracebacks
        else '  "files_to_fix": [],'
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
  "incident_report": "4-6 sentence human-readable report. Cover: what happened, likely cause, blast radius, and recommended action. Write for an on-call engineer woken at 3am. Reference specific file names and line numbers if tracebacks are present.",{files_to_fix_schema}
}}

Rules:
- severity must be exactly: low | medium | high | critical
- confidence must be a float 0.0–1.0
- Be specific — reference the actual metric values and service name above
- If tracebacks are present, always reference the exact file:line in root_cause and recommended_action
- incident_report must be readable without any context beyond this JSON
"""


# ── analyzer ─────────────────────────────────────────────────────────────────

class GroqAnalyzer:
    """
    Calls Groq API and returns a structured IncidentAnalysis.
    Falls back to rule-based analysis if API is unavailable.
    """

    def __init__(
        self,
        api_key:  Optional[str] = None,
        model:    str = GROQ_MODEL,
        timeout:  int = 30,
    ):
        if api_key is None:
            self._api_key = os.getenv("GROQ_API_KEY", "")
        else:
            self._api_key = api_key
        self._model   = model
        self._timeout = aiohttp.ClientTimeout(total=timeout)

        if not self._api_key:
            logger.warning(
                "[GroqAnalyzer] GROQ_API_KEY not set — "
                "will use rule-based fallback for all analyses"
            )

    @property
    def available(self) -> bool:
        return bool(self._api_key)

    async def analyze(
        self,
        service:   str,
        anomalies: list[Anomaly],
        metrics:   list[Any],
        logs:      list[Any],
    ) -> IncidentAnalysis:
        """
        Analyze anomalies and return a structured IncidentAnalysis.
        Never raises — falls back gracefully if Groq is unavailable.
        """
        if not self.available:
            return self._fallback(anomalies, logs, service)

        try:
            return await self._call_groq(service, anomalies, metrics, logs)
        except Exception as exc:
            logger.error(
                "[GroqAnalyzer] API call failed (%s) — using fallback", exc
            )
            return self._fallback(anomalies, logs, service)

    # ── private ───────────────────────────────────────────────────────────────

    async def _call_groq(
        self,
        service:   str,
        anomalies: list[Anomaly],
        metrics:   list[Any],
        logs:      list[Any],
    ) -> IncidentAnalysis:
        prompt = _build_prompt(
            service   = service,
            anomalies = anomalies,
            metrics   = metrics,
            logs      = logs,
            now       = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
        )

        payload = {
            "model":       self._model,
            "temperature": 0.1,
            "max_tokens":  2048,
            "messages":    [{"role": "user", "content": prompt}],
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type":  "application/json",
        }

        async with aiohttp.ClientSession(timeout=self._timeout) as s:
            async with s.post(GROQ_URL, json=payload, headers=headers) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise RuntimeError(f"Groq [{resp.status}]: {text}")
                data = await resp.json()

        raw = data["choices"][0]["message"]["content"].strip()
        logger.debug("[GroqAnalyzer] Raw response: %s", raw[:200])
        return self._parse(raw)

    def _parse(self, raw: str) -> IncidentAnalysis:
        clean = raw
        if clean.startswith("```"):
            clean = "\n".join(
                l for l in clean.split("\n")
                if not l.strip().startswith("```")
            ).strip()

        parsed = json.loads(clean)

        severity   = _SEVERITY_MAP.get(
            parsed.get("severity", "medium").lower(), Severity.MEDIUM
        )
        confidence = float(parsed.get("confidence", 0.7))
        confidence = max(0.0, min(1.0, confidence))

        # files_to_fix: validate and normalise each entry
        raw_files = parsed.get("files_to_fix", [])
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
            model        = self._model,
            fallback     = False,
        )

    def _fallback(
        self,
        anomalies: list[Anomaly],
        logs:      list[Any],
        service:   str,
    ) -> IncidentAnalysis:
        """Rule-based fallback — extracts fix locations from log metadata."""
        from agents.monitoring_agent.incident_factory import _SEVERITY_ORDER

        worst = max(
            anomalies,
            key=lambda a: _SEVERITY_ORDER.index(a.severity),
        )
        sev = worst.severity
        msg = worst.message

        # Extract files_to_fix from Log.metadata (populated by FileCollector)
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
            recommended  = (
                "Investigate recent deployments and check downstream dependencies."
                + fix_summary
            ),
            confidence   = 0.6,
            report       = (
                f"{service} is experiencing an incident. "
                f"{msg}."
                f"{fix_summary} "
                f"Severity has been assessed as {sev.value}. "
                f"Investigate recent deployments and check downstream dependencies. "
                f"This assessment was generated by rule-based fallback "
                f"(GROQ_API_KEY not set or API unavailable)."
            ),
            files_to_fix = files_to_fix,
            model        = "fallback",
            fallback     = True,
        )