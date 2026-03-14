"""
core/orchestrator.py
---------------------
The Orchestrator is the brain of the system.
It coordinates all Agents, manages incident workflows,
and ensures the right Agent is called at the right time.

Workflow:
    Incident Created
         │
         ▼
    Knowledge Agent  →  Solution
         │
         ▼
    Self-Healing Agent  →  Remediation
         │
         ▼
    Alerting Agent  →  Notification
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional

from core.models import (
    AgentStatus,
    Incident,
    IncidentStatus,
    Solution,
)
from core.event_bus import EventBus, Event, EventType
from core.state_manager import StateManager
from core.context_manager import ContextManager
from core.agent_registery import AgentRegistry

logger = logging.getLogger(__name__)


class Orchestrator:
    """
    The Orchestrator coordinates all Agents in the system.

    Responsibilities:
        - Register and manage Agents
        - Listen for Events from the EventBus
        - Trigger the correct Agent for each Event
        - Track incident workflow from detection to resolution

    Example:
        orchestrator = Orchestrator()

        # Register agents
        orchestrator.register_agent("monitoring_agent", monitoring_agent)
        orchestrator.register_agent("knowledge_agent", knowledge_agent)
        orchestrator.register_agent("self_healing_agent", healing_agent)
        orchestrator.register_agent("alerting_agent", alerting_agent)

        # Start the system
        await orchestrator.start()
    """

    def __init__(self):
        self.event_bus       = EventBus()
        self.state_manager   = StateManager()
        self.context_manager = ContextManager()
        self.registry        = AgentRegistry()
        self._running        = False

        self._subscribe_to_events()

        logger.info("Orchestrator initialized")

    # ============================================================
    # Setup
    # ============================================================

    def _subscribe_to_events(self) -> None:
        """Subscribe to all core EventBus events."""
        self.event_bus.subscribe(
            EventType.INCIDENT_CREATED,
            self._on_incident_created,
        )
        self.event_bus.subscribe(
            EventType.INVESTIGATION_COMPLETE,
            self._on_investigation_complete,
        )
        self.event_bus.subscribe(
            EventType.REMEDIATION_COMPLETE,
            self._on_remediation_complete,
        )
        self.event_bus.subscribe(
            EventType.REMEDIATION_FAILED,
            self._on_remediation_failed,
        )

    def register_agent(
        self,
        name: str,
        agent: object,
        metadata: Optional[dict] = None,
    ) -> None:
        """Register an Agent with the Orchestrator."""
        self.registry.register(name, agent, metadata)
        self.state_manager.set_agent_status(name, AgentStatus.IDLE)
        logger.info(f"[Orchestrator] Agent registered: '{name}'")

    # ============================================================
    # Lifecycle
    # ============================================================

    async def start(self) -> None:
        """Start the Orchestrator."""
        self._running = True
        logger.info("[Orchestrator] Started")

    async def stop(self) -> None:
        """Stop the Orchestrator and all Agents."""
        self._running = False

        for record in self.registry.get_all():
            self.state_manager.set_agent_status(
                record.name, AgentStatus.STOPPED
            )

        logger.info("[Orchestrator] Stopped")

    # ============================================================
    # Incident Workflow
    # ============================================================

    async def handle_incident(self, incident: Incident) -> None:
        """
        Main entry point — trigger the full incident workflow.

        Steps:
            1. Save incident to state
            2. Create context
            3. Publish INCIDENT_CREATED event
            4. Knowledge Agent investigates
            5. Self-Healing Agent remediates
            6. Alerting Agent notifies
        """
        logger.info(f"[Orchestrator] Handling: {incident}")

        # Step 1 — Save to state
        self.state_manager.add_incident(incident)
        self.state_manager.update_incident_status(
            incident.incident_id, IncidentStatus.INVESTIGATING
        )

        # Step 2 — Create context
        self.context_manager.create_context(incident)

        # Step 3 — Publish event
        await self.event_bus.publish(Event(
            type=EventType.INCIDENT_CREATED,
            source="orchestrator",
            incident_id=incident.incident_id,
            data={
                "incident_id": incident.incident_id,
                "service"    : incident.service,
                "severity"   : incident.severity.value,
                "description": incident.description,
            }
        ))

    # ============================================================
    # Event Handlers
    # ============================================================

    async def _on_incident_created(self, event: Event) -> None:
        """Triggered when a new Incident is created — call Knowledge Agent."""
        logger.info(f"[Orchestrator] Incident created → calling Knowledge Agent")

        knowledge_agent = self.registry.get_agent("knowledge_agent")
        if not knowledge_agent:
            logger.error("[Orchestrator] Knowledge Agent not registered!")
            return

        self.state_manager.set_agent_status(
            "knowledge_agent", AgentStatus.RUNNING
        )

        context = self.context_manager.get_context(event.incident_id)

        try:
            solution = await knowledge_agent.investigate(context)

            if solution:
                self.state_manager.add_solution(solution)

                await self.event_bus.publish(Event(
                    type=EventType.INVESTIGATION_COMPLETE,
                    source="knowledge_agent",
                    incident_id=event.incident_id,
                    data={"solution": solution},
                ))

        except Exception as e:
            logger.error(f"[Orchestrator] Knowledge Agent failed: {e}")

        finally:
            self.state_manager.set_agent_status(
                "knowledge_agent", AgentStatus.IDLE
            )

    async def _on_investigation_complete(self, event: Event) -> None:
        """Triggered when investigation is done — call Self-Healing Agent."""
        logger.info(f"[Orchestrator] Investigation complete → calling Self-Healing Agent")

        healing_agent = self.registry.get_agent("self_healing_agent")
        if not healing_agent:
            logger.error("[Orchestrator] Self-Healing Agent not registered!")
            return

        self.state_manager.set_agent_status(
            "self_healing_agent", AgentStatus.RUNNING
        )

        solution: Solution = event.data.get("solution")

        self.state_manager.update_incident_status(
            event.incident_id, IncidentStatus.REMEDIATING
        )

        try:
            await healing_agent.remediate(solution)

        except Exception as e:
            logger.error(f"[Orchestrator] Self-Healing Agent failed: {e}")

        finally:
            self.state_manager.set_agent_status(
                "self_healing_agent", AgentStatus.IDLE
            )

    async def _on_remediation_complete(self, event: Event) -> None:
        """Triggered when remediation succeeds — resolve incident and notify."""
        logger.info(f"[Orchestrator] Remediation complete → resolving incident")

        self.state_manager.update_incident_status(
            event.incident_id, IncidentStatus.RESOLVED
        )

        await self._send_alert(
            incident_id=event.incident_id,
            title="Incident Resolved",
            message=f"Incident {event.incident_id} has been resolved automatically.",
        )

        self.context_manager.drop_context(event.incident_id)

    async def _on_remediation_failed(self, event: Event) -> None:
        """Triggered when remediation fails — mark failed and notify."""
        logger.warning(f"[Orchestrator] Remediation failed for {event.incident_id}")

        self.state_manager.update_incident_status(
            event.incident_id, IncidentStatus.FAILED
        )

        await self._send_alert(
            incident_id=event.incident_id,
            title="Remediation Failed",
            message=f"Incident {event.incident_id} could not be resolved automatically. Manual intervention required.",
        )

    # ============================================================
    # Helpers
    # ============================================================

    async def _send_alert(
        self,
        incident_id: str,
        title: str,
        message: str,
    ) -> None:
        """Send an alert via the Alerting Agent if available."""
        alerting_agent = self.registry.get_agent("alerting_agent")
        if not alerting_agent:
            logger.warning("[Orchestrator] Alerting Agent not registered — skipping alert")
            return

        try:
            await alerting_agent.send(
                incident_id=incident_id,
                title=title,
                message=message,
            )
        except Exception as e:
            logger.error(f"[Orchestrator] Alerting Agent failed: {e}")

    # ============================================================
    # Summary
    # ============================================================

    def summary(self) -> dict:
        """Return a full system summary."""
        return {
            "orchestrator" : "running" if self._running else "stopped",
            "agents"       : self.registry.summary(),
            "state"        : self.state_manager.summary(),
            "event_history": len(self.event_bus.get_history()),
        }

    def __repr__(self):
        return (
            f"Orchestrator("
            f"running={self._running}, "
            f"agents={self.registry.get_all_names()})"
        )