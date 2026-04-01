import pytest

@pytest.mark.asyncio
async def test_full_pipeline():
    from agents.monitoring_agent.file_collector import FileCollector
    from agents.monitoring_agent.detector import Detector
    from agents.monitoring_agent.config import ThresholdConfig
    from agents.monitoring_agent.incident_factory import IncidentFactory

    # Step 1 — fake log
    log_text = """
Traceback (most recent call last):
  File "deploy.py", line 47, in run_pipeline
KeyError: 'AWS_REGION'
"""

    # Step 2 — parse
    from agents.monitoring_agent.log_parser import LogParser
    parser = LogParser()
    result = parser._parse_text(log_text, "auth-api", "test.log")

    # Step 3 — build metrics manually
    metrics = [
        type("M", (), {"name": "traceback_count", "value": 1})()
    ]

    # Step 4 — build logs (simulate collector output)
    logs = []
    for err in result.errors:
        logs.append(type("L", (), {
            "message": err.message,
            "level": "ERROR",
            "metadata": {
                "file": err.file,
                "line": err.line,
                "function": err.function,
                "fix_here": f"{err.file}:{err.line}"
            }
        })())

    # Step 5 — detect
    detector = Detector(ThresholdConfig())
    anomalies = detector.analyze("auth-api", metrics, logs)

    assert anomalies, "No anomaly detected ❌"

    # Step 6 — incident
    factory = IncidentFactory()
    incident = factory.create("auth-api", anomalies, metrics, logs)

    assert incident.severity is not None

    print("✅ INCIDENT:", incident.description)
    print("🔥 FIX:", logs[0].metadata["fix_here"])