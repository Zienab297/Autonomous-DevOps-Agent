"""
The MonitoringAgent polls services on a fixed interval,
detects anomalies, and publishes INCIDENT_CREATED events
to the EventBus when something goes wrong.

Lifecycle
---------
    agent = MonitoringAgent(bus, registry, config)
    await agent.start()   # registers, starts poll loop
    ...
    await agent.stop()    # cancels loop, unregisters

Event flow
----------
    [poll loop]
        collector.collect_metrics(service) → List[Metric]
        collector.collect_logs(service)    → List[Log]
        detector.analyze(...)              → List[Anomaly]
        incident_factory.create(...)       → Incident
        groq_analyzer.analyze(...)         → IncidentAnalysis  ← LLM step
            patches Incident.severity, description, metadata
        EventBus.publish(INCIDENT_CREATED)
"""

import asyncio
import logging
from typing import Optional

from core.base_agent import BaseAgent, AgentEvent
from core.event_bus import EventBus, Event, EventType
from core.agent_registery import AgentRegistry
from core.context_manager import ContextManager

from agents.monitoring_agent.config import MonitoringConfig
from agents.monitoring_agent.collector import BaseCollector, MockCollector
from agents.monitoring_agent.detector import Detector
from agents.monitoring_agent.incident_factory import IncidentFactory
from agents.monitoring_agent.groq_analyzer import GroqAnalyzer

logger = logging.getLogger(__name__)


class MonitoringAgent(BaseAgent):
    """
    Polls all configured services and fires INCIDENT_CREATED events
    when anomalies are detected.

    Example:
        config = MonitoringConfig(
            services=["auth-api", "payments-api"],
            poll_interval=30.0,
            collector_backend="mock",
        )
        agent = MonitoringAgent(
            event_bus=bus,
            registry=registry,
            config=config,
        )
        await agent.start()
    """

    def __init__(
        self,
        event_bus       : EventBus,
        registry        : AgentRegistry,
        config          : Optional[MonitoringConfig] = None,
        collector       : Optional[BaseCollector] = None,
        context_manager : Optional[ContextManager] = None,
        state_manager   = None,
        groq_api_key    : Optional[str] = None,
    ):
        super().__init__(
            name       = "monitoring_agent",
            event_bus  = event_bus,
            registry   = registry,
        )
        self._config          = config or MonitoringConfig()
        self._context_manager = context_manager
        self._state_manager   = state_manager

        # Collector: use the injected one, or build from config
        self._collector = collector or self._build_collector()

        # Stateless helpers — created once, reused every poll
        self._detector = Detector(self._config.thresholds)
        self._factory  = IncidentFactory()

        # Groq LLM analyzer — enriches incidents before they are published
        self._analyzer = GroqAnalyzer(api_key=groq_api_key)

        # Background polling task
        self._poll_task: Optional[asyncio.Task] = None

        # Track which incidents we've already created this session
        # (service → incident_id) to avoid duplicate incidents for
        # the same ongoing anomaly
        self._active_incidents: dict[str, str] = {}

    # --------------------------------------------------------
    # BaseAgent lifecycle hooks
    # --------------------------------------------------------

    async def _setup(self) -> None:
        """Start the background polling loop."""
        self._poll_task = asyncio.create_task(
            self._poll_loop(),
            name="monitoring_agent_poll_loop",
        )
        self.logger.info(
            "[MonitoringAgent] Poll loop started "
            "(interval=%.1fs, services=%s, backend=%s, groq=%s)",
            self._config.poll_interval,
            self._config.services,
            self._config.collector_backend,
            "enabled" if self._analyzer.available else "fallback",
        )

    async def _teardown(self) -> None:
        """Cancel the polling loop gracefully."""
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        self.logger.info("[MonitoringAgent] Poll loop stopped")

    async def handle_event(self, event: AgentEvent) -> None:
        """
        The MonitoringAgent is a producer, not a consumer.
        This is a no-op unless you later subscribe to control events
        (e.g. EventType.INCIDENT_RESOLVED → clear _active_incidents).
        """

    # --------------------------------------------------------
    # Polling loop
    # --------------------------------------------------------

    async def _poll_loop(self) -> None:
        """
        Main loop: poll every service, detect anomalies, publish incidents.
        Runs until the agent is stopped.
        """
        self.logger.info("[MonitoringAgent] First poll starting...")

        while True:
            try:
                await self._poll_all_services()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.logger.error(
                    "[MonitoringAgent] Unexpected error in poll loop: %s",
                    e, exc_info=True,
                )

            try:
                await asyncio.sleep(self._config.poll_interval)
            except asyncio.CancelledError:
                break

    async def _poll_all_services(self) -> None:
        """Poll all configured services concurrently."""
        tasks = [
            self._poll_service(service)
            for service in self._config.services
        ]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _poll_service(self, service: str) -> None:
        """
        Full poll cycle for a single service:
            collect → detect → maybe create incident → maybe publish event.
        """
        try:
            # 1. Collect
            metrics = await self._collector.collect_metrics(service)
            logs    = await self._collector.collect_logs(
                service, max_lines=self._config.max_log_lines
            )

            self.logger.debug(
                "[MonitoringAgent] Polled %s: %d metrics, %d logs",
                service, len(metrics), len(logs),
            )

            # 2. Detect anomalies
            anomalies = self._detector.analyze(service, metrics, logs)

            if not anomalies:
                # Service is healthy — clear any active incident tracking
                if service in self._active_incidents:
                    self.logger.info(
                        "[MonitoringAgent] %s returned to healthy — "
                        "clearing active incident %s",
                        service, self._active_incidents[service],
                    )
                    del self._active_incidents[service]
                return

            # 3. Avoid flooding — one active incident per service at a time
            if service in self._active_incidents:
                self.logger.debug(
                    "[MonitoringAgent] %s already has active incident %s — skipping",
                    service, self._active_incidents[service],
                )
                return

            # 4. Build incident (rule-based severity + description)
            incident = self._factory.create(
                service   = service,
                anomalies = anomalies,
                metrics   = metrics,
                logs      = logs,
            )

            # 5. Enrich with Groq LLM — replaces severity, description,
            #    adds full report to metadata. Falls back gracefully.
            analysis = await self._analyzer.analyze(
                service   = service,
                anomalies = anomalies,
                metrics   = metrics,
                logs      = logs,
            )
            incident.severity    = analysis.severity
            incident.description = analysis.root_cause
            incident.metadata["llm_analysis"] = {
                "model":       analysis.model,
                "severity":    analysis.severity.value,
                "root_cause":  analysis.root_cause,
                "impact":      analysis.impact,
                "recommended": analysis.recommended,
                "confidence":  analysis.confidence,
                "report":      analysis.report,
                "fallback":    analysis.fallback,
            }
            self.logger.info(
                "[MonitoringAgent] Groq: severity=%s confidence=%.0f%% fallback=%s",
                analysis.severity.value, analysis.confidence * 100, analysis.fallback,
            )

            # 6. Track it
            self._active_incidents[service] = incident.incident_id

            # 7. Store in StateManager
            if self._state_manager:
                self._state_manager.add_incident(incident)

            # 8. Store in ContextManager
            if self._context_manager:
                self._context_manager.create_context(incident)
                self._context_manager.add_metrics(incident.incident_id, metrics)
                self._context_manager.add_logs(incident.incident_id, logs)

            # 9. Publish event → Orchestrator picks it up
            await self.publish(Event(
                type        = EventType.INCIDENT_CREATED,
                source      = self.name,
                incident_id = incident.incident_id,
                data        = {
                    "incident_id"  : incident.incident_id,
                    "service"      : incident.service,
                    "severity"     : incident.severity.value,
                    "description"  : incident.description,
                    "impact"       : analysis.impact,
                    "recommended"  : analysis.recommended,
                    "confidence"   : analysis.confidence,
                    "report"       : analysis.report,
                    "anomaly_count": len(anomalies),
                    "llm_fallback" : analysis.fallback,
                },
            ))

            self.logger.warning(
                "[MonitoringAgent] INCIDENT CREATED: %s [%s] — %s",
                incident.incident_id,
                incident.severity.value.upper(),
                incident.service,
            )
            self.logger.warning(
                "[MonitoringAgent] INCIDENT REPORT:\n%s", analysis.report
            )

        except Exception as e:
            self.logger.error(
                "[MonitoringAgent] Error polling %s: %s",
                service, e, exc_info=True,
            )

    # --------------------------------------------------------
    # Helpers
    # --------------------------------------------------------

    def _build_collector(self) -> BaseCollector:
        """Instantiate the correct collector from config."""
        backend = self._config.collector_backend

        if backend == "mock":
            return MockCollector()

        # Future backends — raise clearly so the developer knows what to add
        raise ValueError(
            f"Unknown collector backend: '{backend}'. "
            f"Supported: 'mock'. "
            f"Add PrometheusCollector / DatadogCollector in collector.py."
        )

    # --------------------------------------------------------
    # Introspection
    # --------------------------------------------------------

    @property
    def active_incidents(self) -> dict[str, str]:
        """Return the current service → incident_id mapping."""
        return dict(self._active_incidents)

    def get_info(self) -> dict:
        info = super().get_info()
        info.update({
            "services"        : self._config.services,
            "poll_interval"   : self._config.poll_interval,
            "backend"         : self._config.collector_backend,
            "active_incidents": self._active_incidents,
            "groq_enabled"    : self._analyzer.available,
            "groq_model"      : self._analyzer._model,
        })
        return info