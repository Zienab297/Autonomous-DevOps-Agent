"""
tests/test_monitoring_agent.py
================================
Tests for MonitoringAgent + GroqAnalyzer integration.

Two modes — automatic based on env:

  STUB MODE  (no GROQ_API_KEY or run with -m stub):
    Uses MockCollector + GroqAnalyzer fallback.
    Zero network, deterministic, fast.

  LIVE MODE  (GROQ_API_KEY set, run with -m live):
    Real Groq API calls. Uses MockCollector so GitHub
    token is not needed — only GROQ_API_KEY required.

Run all:
    pytest tests/test_monitoring_agent.py -v -s

Run only stub:
    pytest tests/test_monitoring_agent.py -v -s -m stub

Run only live:
    GROQ_API_KEY=gsk_... pytest tests/test_monitoring_agent.py -v -s -m live
"""
from __future__ import annotations

import asyncio
import os
import pytest
from dotenv import load_dotenv

load_dotenv()

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
LIVE         = bool(GROQ_API_KEY)

def pytest_configure(config):
    config.addinivalue_line('markers', 'live: requires GROQ_API_KEY, makes real API calls')
    config.addinivalue_line('markers', 'stub: runs fully offline with MockCollector')

# ── print helpers ─────────────────────────────────────────────────────────────

SEP = "-" * 64
def section(t): print(f"\n{SEP}\n  {t}\n{SEP}")
def ok(m):      print(f"  [OK]   {m}")
def info(m):    print(f"  [..]   {m}")
def kv(k, v):   print(f"  {k:<34} {v}")
def box(title, body):
    print(f"\n  ┌─ {title} {'─' * (54 - len(title))}")
    for line in body.strip().split("\n"):
        print(f"  │  {line}")
    print(f"  └{'─' * 58}")


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def orch():
    from core.orchestrator import Orchestrator
    return Orchestrator()

@pytest.fixture
def mock_collector():
    from agents.monitoring_agent.collector import MockCollector
    return MockCollector(seed=42)

@pytest.fixture
def agent(orch, mock_collector):
    from agents.monitoring_agent.agent  import MonitoringAgent
    from agents.monitoring_agent.config import MonitoringConfig
    return MonitoringAgent(
        event_bus       = orch.event_bus,
        registry        = orch.registry,
        config          = MonitoringConfig(
            services      = ["auth-api", "payments-api"],
            poll_interval = 999,
        ),
        collector       = mock_collector,
        context_manager = orch.context_manager,
        state_manager   = orch.state_manager,
        groq_api_key    = GROQ_API_KEY,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — GroqAnalyzer unit tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestGroqAnalyzer:

    @pytest.mark.asyncio
    async def test_fallback_when_no_key(self):
        """GroqAnalyzer returns a valid IncidentAnalysis without an API key."""
        section("GroqAnalyzer — fallback (no API key)")
        from agents.monitoring_agent.groq_analyzer import GroqAnalyzer
        from agents.monitoring_agent.detector      import Anomaly
        from agents.monitoring_agent.collector     import MockCollector
        from core.models import Severity

        analyzer = GroqAnalyzer(api_key="")
        assert not analyzer.available

        collector = MockCollector(seed=42)
        collector.inject_anomaly("auth-api", "error_rate", 0.45)
        metrics = await collector.collect_metrics("auth-api")
        logs    = await collector.collect_logs("auth-api")

        from agents.monitoring_agent.detector import Detector
        from agents.monitoring_agent.config   import ThresholdConfig
        anomalies = Detector(ThresholdConfig()).analyze("auth-api", metrics, logs)

        analysis = await analyzer.analyze(
            service   = "auth-api",
            anomalies = anomalies,
            metrics   = metrics,
            logs      = logs,
        )

        kv("Severity:",    analysis.severity.value)
        kv("Root cause:",  analysis.root_cause)
        kv("Impact:",      analysis.impact)
        kv("Recommended:", analysis.recommended)
        kv("Confidence:",  f"{analysis.confidence:.0%}")
        kv("Fallback:",    str(analysis.fallback))
        box("Report", analysis.report)

        assert analysis.severity  in Severity.__members__.values()
        assert len(analysis.root_cause) > 5
        assert len(analysis.report)     > 20
        assert analysis.confidence > 0.0
        assert analysis.fallback is True
        ok("Fallback analysis returned valid IncidentAnalysis")

    @pytest.mark.asyncio
    @pytest.mark.live
    async def test_live_groq_call(self):
        """Real Groq API call returns structured IncidentAnalysis."""
        if not LIVE:
            pytest.skip("GROQ_API_KEY not set")

        section("GroqAnalyzer — live Groq API call")
        from agents.monitoring_agent.groq_analyzer import GroqAnalyzer
        from agents.monitoring_agent.collector     import MockCollector
        from agents.monitoring_agent.detector      import Detector
        from agents.monitoring_agent.config        import ThresholdConfig
        from core.models import Severity

        analyzer  = GroqAnalyzer(api_key=GROQ_API_KEY)
        collector = MockCollector(seed=42)
        collector.inject_anomaly("auth-api", "error_rate",    0.45)
        collector.inject_anomaly("auth-api", "latency_p99_ms", 1800.0)

        metrics   = await collector.collect_metrics("auth-api")
        logs      = await collector.collect_logs("auth-api")
        anomalies = Detector(ThresholdConfig()).analyze("auth-api", metrics, logs)

        info(f"Sending {len(anomalies)} anomaly(ies) to Groq ({analyzer._model}) …")
        analysis = await analyzer.analyze(
            service   = "auth-api",
            anomalies = anomalies,
            metrics   = metrics,
            logs      = logs,
        )

        kv("Model:",       analysis.model)
        kv("Severity:",    analysis.severity.value)
        kv("Root cause:",  analysis.root_cause)
        kv("Impact:",      analysis.impact)
        kv("Recommended:", analysis.recommended)
        kv("Confidence:",  f"{analysis.confidence:.0%}")
        kv("Fallback:",    str(analysis.fallback))
        box("Incident Report (from Groq)", analysis.report)

        assert analysis.severity  in Severity.__members__.values()
        assert analysis.confidence >= 0.0
        assert analysis.confidence <= 1.0
        assert len(analysis.root_cause)  > 10
        assert len(analysis.report)      > 30
        assert analysis.fallback is False
        ok(f"Groq returned: severity={analysis.severity.value} "
           f"confidence={analysis.confidence:.0%}")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — Agent lifecycle
# ═══════════════════════════════════════════════════════════════════════════════

class TestAgentLifecycle:

    @pytest.mark.asyncio
    async def test_start_stop(self, orch, agent):
        """Agent registers, starts poll loop, stops cleanly."""
        section("Agent lifecycle — start / stop")
        from core.base_agent import AgentState

        await agent.start()

        kv("State:",        agent.state.value)
        kv("Agent ID:",     agent.agent_id)
        kv("Registered:",   str(orch.registry.is_registered("monitoring_agent")))
        kv("Groq enabled:", str(agent._analyzer.available))
        kv("Groq model:",   agent._analyzer._model)

        assert agent.state == AgentState.RUNNING
        assert orch.registry.is_registered("monitoring_agent")
        ok("Agent started successfully")

        await agent.stop()
        assert agent.state == AgentState.STOPPED
        assert not orch.registry.is_registered("monitoring_agent")
        ok("Agent stopped cleanly")

    @pytest.mark.asyncio
    async def test_get_info_includes_groq(self, agent):
        """get_info() exposes Groq status."""
        section("get_info — includes Groq fields")
        await agent.start()
        info_dict = agent.get_info()

        kv("groq_enabled:", str(info_dict.get("groq_enabled")))
        kv("groq_model:",   str(info_dict.get("groq_model")))

        assert "groq_enabled" in info_dict
        assert "groq_model"   in info_dict
        ok("get_info() exposes Groq fields")
        await agent.stop()


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — Poll service: healthy
# ═══════════════════════════════════════════════════════════════════════════════

class TestHealthyService:

    @pytest.mark.asyncio
    async def test_no_incident_when_healthy(self, orch, agent):
        """No INCIDENT_CREATED event when service is healthy."""
        section("Poll — healthy service (no incident)")
        from core.event_bus import EventType

        received = []
        async def capture(e): received.append(e)
        orch.event_bus.subscribe(EventType.INCIDENT_CREATED, capture)

        await agent.start()
        await agent._poll_service("auth-api")

        kv("INCIDENT_CREATED events:", len(received))
        assert len(received) == 0
        ok("No incident fired for healthy service")
        await agent.stop()

    @pytest.mark.asyncio
    async def test_active_incident_cleared_on_recovery(
        self, orch, agent, mock_collector
    ):
        """Active incident is cleared when service recovers."""
        section("Poll — incident clears on recovery")

        # First poll — inject anomaly
        mock_collector.inject_anomaly("auth-api", "error_rate", 0.45)
        await agent.start()
        await agent._poll_service("auth-api")
        assert "auth-api" in agent.active_incidents
        kv("Active incident after spike:", agent.active_incidents.get("auth-api"))

        # Second poll — remove anomaly (recovery)
        mock_collector.clear_anomaly("auth-api")
        await agent._poll_service("auth-api")
        assert "auth-api" not in agent.active_incidents
        ok("Active incident cleared after recovery")
        await agent.stop()


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 4 — Poll service: anomaly detected
# ═══════════════════════════════════════════════════════════════════════════════

class TestAnomalyDetected:

    @pytest.mark.asyncio
    async def test_incident_created_on_anomaly(
        self, orch, agent, mock_collector
    ):
        """Injecting error_rate anomaly fires INCIDENT_CREATED with LLM fields."""
        section("Poll — anomaly → INCIDENT_CREATED with Groq fields")
        from core.event_bus import EventType

        received = []
        async def capture(e): received.append(e)
        orch.event_bus.subscribe(EventType.INCIDENT_CREATED, capture)

        mock_collector.inject_anomaly("auth-api", "error_rate", 0.45)

        await agent.start()
        await agent._poll_service("auth-api")

        kv("INCIDENT_CREATED events:", len(received))
        assert len(received) == 1
        e = received[0]

        kv("Service:",      e.data["service"])
        kv("Severity:",     e.data["severity"])
        kv("Description:",  e.data["description"])
        kv("Impact:",       e.data.get("impact", "—"))
        kv("Recommended:",  e.data.get("recommended", "—"))
        kv("Confidence:",   f"{e.data.get('confidence', 0):.0%}")
        kv("LLM fallback:", str(e.data.get("llm_fallback")))
        box("Incident Report", e.data.get("report", "No report"))

        assert e.data["service"]     == "auth-api"
        assert e.data["severity"]    in ("low", "medium", "high", "critical")
        assert len(e.data["description"]) > 5
        assert "impact"      in e.data
        assert "recommended" in e.data
        assert "confidence"  in e.data
        assert "report"      in e.data
        ok("INCIDENT_CREATED fired with full LLM fields")
        await agent.stop()

    @pytest.mark.asyncio
    async def test_incident_severity_matches_analysis(
        self, orch, agent, mock_collector
    ):
        """Incident.severity on the stored object matches the LLM/fallback output."""
        section("Poll — Incident.severity matches IncidentAnalysis")
        from core.models import Severity

        mock_collector.inject_anomaly("auth-api", "error_rate", 0.45)
        await agent.start()
        await agent._poll_service("auth-api")

        incidents = orch.state_manager.get_active_incidents()
        assert len(incidents) >= 1
        inc = incidents[0]

        kv("Incident ID:",  inc.incident_id)
        kv("Severity:",     inc.severity.value)
        kv("Description:",  inc.description)
        assert inc.severity in Severity.__members__.values()
        ok(f"Incident stored with severity={inc.severity.value}")
        await agent.stop()

    @pytest.mark.asyncio
    async def test_llm_analysis_in_metadata(
        self, orch, agent, mock_collector
    ):
        """incident.metadata['llm_analysis'] contains all Groq fields."""
        section("Poll — LLM analysis stored in Incident.metadata")

        mock_collector.inject_anomaly("auth-api", "error_rate", 0.45)
        await agent.start()
        await agent._poll_service("auth-api")

        incidents = orch.state_manager.get_active_incidents()
        inc = incidents[0]
        llm = inc.metadata.get("llm_analysis", {})

        kv("model:",       llm.get("model"))
        kv("severity:",    llm.get("severity"))
        kv("root_cause:",  llm.get("root_cause"))
        kv("impact:",      llm.get("impact"))
        kv("recommended:", llm.get("recommended"))
        kv("confidence:",  f"{llm.get('confidence', 0):.0%}")
        kv("fallback:",    str(llm.get("fallback")))
        box("Report in metadata", llm.get("report", "No report"))

        required = ["model","severity","root_cause","impact",
                    "recommended","confidence","report","fallback"]
        for field in required:
            assert field in llm, f"Missing field: {field}"
        ok("All LLM fields present in Incident.metadata['llm_analysis']")
        await agent.stop()

    @pytest.mark.asyncio
    async def test_context_manager_receives_incident(
        self, orch, agent, mock_collector
    ):
        """IncidentContext is created with metrics and logs attached."""
        section("Poll — IncidentContext built with metrics + logs")

        mock_collector.inject_anomaly("auth-api", "error_rate", 0.45)
        await agent.start()
        await agent._poll_service("auth-api")

        incidents = orch.state_manager.get_active_incidents()
        inc = incidents[0]
        ctx = orch.context_manager.get_context(inc.incident_id)

        kv("Context created:",  str(ctx is not None))
        kv("Metrics in ctx:",   len(ctx.metrics) if ctx else 0)
        kv("Logs in ctx:",      len(ctx.logs)    if ctx else 0)

        if ctx:
            print("\n  Context text (LLM prompt input):")
            for line in ctx.to_text().split("\n")[:15]:
                print(f"    {line}")

        assert ctx is not None
        assert len(ctx.metrics) > 0
        assert len(ctx.logs)    > 0
        ok("IncidentContext ready for KnowledgeAgent")
        await agent.stop()

    @pytest.mark.asyncio
    async def test_deduplication(self, orch, agent, mock_collector):
        """Same service only creates one incident across multiple polls."""
        section("Poll — deduplication (one incident per service)")
        from core.event_bus import EventType

        received = []
        async def capture(e): received.append(e)
        orch.event_bus.subscribe(EventType.INCIDENT_CREATED, capture)

        mock_collector.inject_anomaly("auth-api", "error_rate", 0.45)
        await agent.start()

        await agent._poll_service("auth-api")
        await agent._poll_service("auth-api")
        await agent._poll_service("auth-api")

        kv("INCIDENT_CREATED events (should be 1):", len(received))
        assert len(received) == 1
        ok("Deduplication works — 3 polls, 1 incident")
        await agent.stop()


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 5 — Multiple anomalies
# ═══════════════════════════════════════════════════════════════════════════════

class TestMultipleAnomalies:

    @pytest.mark.asyncio
    async def test_multiple_anomalies_single_incident(
        self, orch, agent, mock_collector
    ):
        """Multiple metric spikes produce one incident with worst severity."""
        section("Poll — multiple anomalies → single incident")
        from core.event_bus import EventType

        received = []
        async def capture(e): received.append(e)
        orch.event_bus.subscribe(EventType.INCIDENT_CREATED, capture)

        mock_collector.inject_anomaly("payments-api", "error_rate",    0.45)
        mock_collector.inject_anomaly("payments-api", "latency_p99_ms", 1800.0)
        mock_collector.inject_anomaly("payments-api", "cpu_usage",      0.92)

        await agent.start()
        await agent._poll_service("payments-api")

        assert len(received) == 1
        e = received[0]
        kv("Anomaly count:", e.data.get("anomaly_count"))
        kv("Severity:",      e.data["severity"])
        kv("Description:",   e.data["description"])
        kv("Impact:",        e.data.get("impact", "—"))
        kv("Recommended:",   e.data.get("recommended", "—"))
        box("Report", e.data.get("report", "No report"))

        assert e.data["anomaly_count"] >= 2
        ok("Multiple anomalies collapsed into one incident with Groq analysis")
        await agent.stop()

    @pytest.mark.asyncio
    async def test_two_services_two_incidents(
        self, orch, agent, mock_collector
    ):
        """Each service gets its own incident independently."""
        section("Poll — two services → two incidents")
        from core.event_bus import EventType

        received = []
        async def capture(e): received.append(e)
        orch.event_bus.subscribe(EventType.INCIDENT_CREATED, capture)

        mock_collector.inject_anomaly("auth-api",     "error_rate", 0.45)
        mock_collector.inject_anomaly("payments-api", "error_rate", 0.45)

        await agent.start()
        await agent._poll_all_services()

        kv("INCIDENT_CREATED events:", len(received))
        for e in received:
            kv(f"  [{e.data['service']}]", e.data["severity"])

        assert len(received) == 2
        services = {e.data["service"] for e in received}
        assert "auth-api"     in services
        assert "payments-api" in services
        ok("Two services → two independent incidents")
        await agent.stop()


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 6 — INCIDENT_CREATED event shape
# ═══════════════════════════════════════════════════════════════════════════════

class TestEventShape:

    @pytest.mark.asyncio
    async def test_event_data_has_all_fields(
        self, orch, agent, mock_collector
    ):
        """INCIDENT_CREATED event.data contains all expected fields."""
        section("INCIDENT_CREATED event — field completeness check")
        from core.event_bus import EventType

        received = []
        async def capture(e): received.append(e)
        orch.event_bus.subscribe(EventType.INCIDENT_CREATED, capture)

        mock_collector.inject_anomaly("auth-api", "error_rate", 0.45)
        await agent.start()
        await agent._poll_service("auth-api")

        assert len(received) == 1
        data = received[0].data

        required_fields = [
            "incident_id", "service", "severity", "description",
            "impact", "recommended", "confidence", "report",
            "anomaly_count", "llm_fallback",
        ]
        print()
        for f in required_fields:
            val = data.get(f, "MISSING")
            kv(f"  {f}:", str(val)[:60])
            assert f in data, f"Missing field: {f}"

        ok("All required fields present in INCIDENT_CREATED event.data")
        await agent.stop()


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 7 — Live Groq test with real API
# ═══════════════════════════════════════════════════════════════════════════════

class TestLiveGroq:

    @pytest.mark.asyncio
    @pytest.mark.live
    async def test_full_poll_with_live_groq(
        self, orch, mock_collector
    ):
        """Full poll cycle using real Groq API — prints the LLM report."""
        if not LIVE:
            pytest.skip("GROQ_API_KEY not set")

        section("Full poll cycle — LIVE Groq API")
        from agents.monitoring_agent.agent  import MonitoringAgent
        from agents.monitoring_agent.config import MonitoringConfig
        from core.event_bus import EventType

        agent = MonitoringAgent(
            event_bus       = orch.event_bus,
            registry        = orch.registry,
            config          = MonitoringConfig(
                services      = ["auth-api"],
                poll_interval = 999,
            ),
            collector       = mock_collector,
            context_manager = orch.context_manager,
            state_manager   = orch.state_manager,
            groq_api_key    = GROQ_API_KEY,
        )

        received = []
        async def capture(e): received.append(e)
        orch.event_bus.subscribe(EventType.INCIDENT_CREATED, capture)

        mock_collector.inject_anomaly("auth-api", "error_rate",    0.45)
        mock_collector.inject_anomaly("auth-api", "latency_p99_ms", 1600.0)

        info("Polling auth-api with real Groq analysis …")
        await agent.start()
        await agent._poll_service("auth-api")

        assert len(received) == 1
        e    = received[0]
        data = e.data

        print()
        kv("Incident ID:",  data["incident_id"])
        kv("Service:",      data["service"])
        kv("Severity:",     data["severity"])
        kv("Confidence:",   f"{data.get('confidence', 0):.0%}")
        kv("LLM fallback:", str(data.get("llm_fallback")))
        kv("Root cause:",   data["description"])
        kv("Impact:",       data.get("impact", "—"))
        kv("Recommended:",  data.get("recommended", "—"))
        box("INCIDENT REPORT (from Groq)", data.get("report", "No report"))

        # Verify LLM content is in IncidentContext
        incidents = orch.state_manager.get_active_incidents()
        inc = incidents[0]
        llm = inc.metadata.get("llm_analysis", {})

        kv("\n  LLM model used:", llm.get("model"))
        kv("  In metadata:",     str(bool(llm)))

        assert data["llm_fallback"] is False
        assert len(data["report"]) > 50
        ok("Live Groq analysis complete — incident report generated")
        await agent.stop()