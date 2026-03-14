"""
Orchestrator - Workflow Director
"""

import logging
from typing import Dict, List, Optional

from .agent_registry import AgentRegistry, AgentStatus
from .context import IncidentContext
from .event_bus import Event, EventBus, EventType
from .models import IncidentStatus, RemediationAction, Solution
from .state_manager import StateManager

logger = logging.getLogger(__name__)

# Required agents that must be running before the system can operate
REQUIRED_AGENTS = [
    "monitoring_agent",
    "knowledge_agent",
    "self_healing_agent",
    "alerting_agent",
]


class Orchestrator:
    """
    Drives the full incident workflow from detection to resolution.

    Example:
        bus      = EventBus()
        state    = StateManager()
        registry = AgentRegistry()

        orchestrator = Orchestrator(
            event_bus=bus,
            state_manager=state,
            agent_registry=registry,
            auto_remediate=True,
            max_retries=3
        )

        orchestrator.start()
    """

    def __init__(
        self,
        event_bus: EventBus,
        state_manager: StateManager,
        agent_registry: AgentRegistry,
        auto_remediate: bool = True,
        max_retries: int = 3,
    ):
        self.bus = event_bus
        self.state = state_manager
        self.registry = agent_registry
        self.auto_remediate = auto_remediate
        self.max_retries = max_retries

        # incident_id -> IncidentContext
        # One context object per active incident
        self._contexts: Dict[str, IncidentContext] = {}

        # incident_id -> retry count
        self._retry_counts: Dict[str, int] = {}

        logger.info("Orchestrator initialized")

    # --------------------------------------------------------
    # Startup
    # --------------------------------------------------------

    def start(self) -> None:
        """
        Subscribe to all workflow Events on the EventBus.
        Must be called once before the system starts.
        """
        self.bus.subscribe(EventType.INCIDENT_CREATED,       self._on_incident_created)
        self.bus.subscribe(EventType.INVESTIGATION_COMPLETE, self._on_investigation_complete)
        self.bus.subscribe(EventType.REMEDIATION_COMPLETE,   self._on_remediation_complete)
        self.bus.subscribe(EventType.REMEDIATION_FAILED,     self._on_remediation_failed)

        logger.info("Orchestrator started — listening for Events")

    def verify_agents(self) -> List[str]:
        """
        Check that all required Agents are registered and running.
        Returns a list of missing Agent names.

        Called after all Agents have been started.

        Example:
            missing = orchestrator.verify_agents()
            if missing:
                raise RuntimeError(f"Missing: {missing}")
        """
        missing = self.registry.verify_required_agents(REQUIRED_AGENTS)
        if missing:
            logger.error(f"Missing required agents: {missing}")
        else:
            logger.info("All required agents verified ✅")
        return missing

    # --------------------------------------------------------
    # Step 1 — Incident detected -> start investigation
    # --------------------------------------------------------

    async def _on_incident_created(self, event: Event) -> None:
        """
        Triggered by: MonitoringAgent publishes INCIDENT_CREATED
        Action: Create an IncidentContext and tell KnowledgeAgent to investigate
        """
        incident_id = event.incident_id
        logger.info(f"[Orchestrator] New incident received: {incident_id}")

        # Fetch the incident from StateManager
        incident = self.state.get_incident(incident_id)
        if not incident:
            logger.error(f"Incident {incident_id} not found in StateManager")
            return

        # Create a fresh context for this incident
        ctx = IncidentContext(incident=incident)
        self._contexts[incident_id] = ctx

        # Update state
        self.state.update_status(incident_id, IncidentStatus.INVESTIGATING)

        # Check KnowledgeAgent is available
        if not self.registry.is_running("knowledge_agent"):
            logger.error("KnowledgeAgent is not running — escalating")
            ctx.escalate()
            await self._send_alert(ctx)
            return

        # Tell KnowledgeAgent to start investigating
        await self.bus.publish(Event(
            type=EventType.INVESTIGATION_STARTED,
            source="orchestrator",
            incident_id=incident_id,
            data={
                "service":      incident.service,
                "description":  incident.description,
                "metrics":      incident.metrics,
                "logs":         incident.logs,
            }
        ))

    # --------------------------------------------------------
    # Step 2 — Investigation done -> remediate or escalate
    # --------------------------------------------------------

    async def _on_investigation_complete(self, event: Event) -> None:
        """
        Triggered by: KnowledgeAgent publishes INVESTIGATION_COMPLETE
        Action:
            - auto_remediate ON  -> tell SelfHealingAgent to fix it
            - auto_remediate OFF -> escalate for human review
        """
        incident_id = event.incident_id
        data = event.data

        logger.info(f"[Orchestrator] Investigation complete: {incident_id}")

        ctx = self._get_context(incident_id)
        if not ctx:
            return

        # Save solution into the context
        solution = Solution(
            incident_id=incident_id,
            root_cause=data.get("root_cause", "Unknown"),
            recommended_action=data.get("recommended_action", RemediationAction.CUSTOM),
            confidence=data.get("confidence", 0.0),
            explanation=data.get("explanation", ""),
            raw_llm_response=data.get("raw_llm_response"),
            retrieved_docs=data.get("retrieved_docs", []),
        )
        ctx.add_solution(solution)

        if self.auto_remediate:
            # Check SelfHealingAgent is available
            if not self.registry.is_running("self_healing_agent"):
                logger.error("SelfHealingAgent is not running — escalating")
                ctx.escalate()
                await self._send_alert(ctx)
                return

            self.state.update_status(incident_id, IncidentStatus.REMEDIATING)

            await self.bus.publish(Event(
                type=EventType.REMEDIATION_STARTED,
                source="orchestrator",
                incident_id=incident_id,
                data={
                    "recommended_action": solution.recommended_action,
                    "confidence":         solution.confidence,
                    "service":            ctx.service,
                }
            ))
        else:
            # Manual mode — escalate for human decision
            logger.info(f"Auto-remediate OFF — escalating: {incident_id}")
            ctx.escalate()
            self.state.update_status(incident_id, IncidentStatus.ESCALATED)
            await self._send_alert(ctx)

    # --------------------------------------------------------
    # Step 3 — Remediation succeeded -> resolve
    # --------------------------------------------------------

    async def _on_remediation_complete(self, event: Event) -> None:
        """
        Triggered by: SelfHealingAgent publishes REMEDIATION_COMPLETE
        Action: Mark resolved and notify team
        """
        incident_id = event.incident_id
        logger.info(f"[Orchestrator] Remediation complete: {incident_id}")

        ctx = self._get_context(incident_id)
        if not ctx:
            return

        self.state.resolve_incident(incident_id)
        self._retry_counts.pop(incident_id, None)

        await self._send_alert(ctx)
        self._cleanup(incident_id)

    # --------------------------------------------------------
    # Step 4 — Remediation failed -> retry or escalate
    # --------------------------------------------------------

    async def _on_remediation_failed(self, event: Event) -> None:
        """
        Triggered by: SelfHealingAgent publishes REMEDIATION_FAILED
        Action:
            - Retries remaining -> retry
            - Retries exhausted -> escalate
        """
        incident_id = event.incident_id
        logger.warning(f"[Orchestrator] Remediation failed: {incident_id}")

        ctx = self._get_context(incident_id)
        if not ctx:
            return

        retries = self._retry_counts.get(incident_id, 0) + 1
        self._retry_counts[incident_id] = retries

        if retries < self.max_retries:
            logger.info(
                f"Retrying remediation for {incident_id} "
                f"({retries}/{self.max_retries})"
            )
            await self.bus.publish(Event(
                type=EventType.REMEDIATION_STARTED,
                source="orchestrator",
                incident_id=incident_id,
                data=event.data
            ))
        else:
            logger.error(
                f"All {self.max_retries} retries failed for {incident_id} — escalating"
            )
            ctx.escalate()
            self.state.update_status(incident_id, IncidentStatus.ESCALATED)
            await self._send_alert(ctx)
            self._cleanup(incident_id)

    # --------------------------------------------------------
    # Helpers
    # --------------------------------------------------------

    async def _send_alert(self, ctx: IncidentContext) -> None:
        """Tell AlertingAgent to send a notification."""
        if not self.registry.is_running("alerting_agent"):
            logger.warning("AlertingAgent is not running — skipping notification")
            return

        await self.bus.publish(Event(
            type=EventType.ALERT_SENT,
            source="orchestrator",
            incident_id=ctx.incident_id,
            data=ctx.summary()
        ))

    def _get_context(self, incident_id: str) -> Optional[IncidentContext]:
        """Retrieve the IncidentContext for an incident. Logs error if missing."""
        ctx = self._contexts.get(incident_id)
        if not ctx:
            logger.error(f"No context found for incident: {incident_id}")
        return ctx

    def _cleanup(self, incident_id: str) -> None:
        """Remove a resolved/escalated incident from active contexts."""
        self._contexts.pop(incident_id, None)
        self._retry_counts.pop(incident_id, None)

    # --------------------------------------------------------
    # Inspection
    # --------------------------------------------------------

    def get_active_incidents(self) -> List[str]:
        """Return IDs of all currently active incidents."""
        return list(self._contexts.keys())

    def get_context(self, incident_id: str) -> Optional[IncidentContext]:
        """Public access to an incident's context."""
        return self._contexts.get(incident_id)

    def __repr__(self):
        return (
            f"Orchestrator("
            f"active_incidents={len(self._contexts)}, "
            f"auto_remediate={self.auto_remediate}, "
            f"max_retries={self.max_retries})"
        )