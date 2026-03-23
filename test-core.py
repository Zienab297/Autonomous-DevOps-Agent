"""
Core Test - Full Incident Workflow Simulation
===============================================
Simulates a complete incident workflow without real Agents.
We manually fire Events to verify the Orchestrator
drives the workflow correctly end to end.

Workflow tested:
    1. MonitoringAgent  -> INCIDENT_CREATED
    2. Orchestrator     -> INVESTIGATION_STARTED
    3. KnowledgeAgent   -> INVESTIGATION_COMPLETE
    4. Orchestrator     -> REMEDIATION_STARTED
    5. SelfHealingAgent -> REMEDIATION_COMPLETE
    6. Orchestrator     -> ALERT_SENT  (resolved)
"""

import asyncio
import sys
sys.path.insert(0, ".")

from core.agent_registery import AgentRegistry, AgentStatus
from core.context_manager import IncidentContext
from core.event_bus import Event, EventBus, EventType
from core.models import IncidentSeverity, IncidentStatus, RemediationAction
from core.orchestrator import Orchestrator
from core.state_manager import StateManager


# ============================================================
# Helpers
# ============================================================

def separator(title: str):
    print(f"\n{'='*55}")
    print(f"  {title}")
    print(f"{'='*55}")

def log(agent: str, msg: str):
    print(f"  [{agent}] {msg}")


# ============================================================
# Fake Agent Handlers
# (Simulating what real Agents would do)
# ============================================================

async def fake_knowledge_agent(event: Event):
    """Simulates KnowledgeAgent receiving INVESTIGATION_STARTED."""
    log("KnowledgeAgent", f"Investigating incident: {event.incident_id}")
    log("KnowledgeAgent", "Running RAG + LLM analysis...")

    # Simulate investigation result
    await bus.publish(Event(
        type=EventType.INVESTIGATION_COMPLETE,
        source="knowledge_agent",
        incident_id=event.incident_id,
        data={
            "root_cause":           "Memory leak in auth-service",
            "recommended_action":   RemediationAction.RESTART_SERVICE,
            "confidence":           0.87,
            "explanation":          "Memory usage reached 98%, OOM kill detected",
            "retrieved_docs":       ["runbook-memory-leak.md", "past-incident-INC-001"],
        }
    ))


async def fake_self_healing_agent(event: Event):
    """Simulates SelfHealingAgent receiving REMEDIATION_STARTED."""
    log("SelfHealingAgent", f"Executing: {event.data.get('recommended_action')}")
    log("SelfHealingAgent", "kubectl rollout restart deployment/auth-api")

    # Simulate successful remediation
    await bus.publish(Event(
        type=EventType.REMEDIATION_COMPLETE,
        source="self_healing_agent",
        incident_id=event.incident_id,
        data={
            "success": True,
            "output":  "deployment.apps/auth-api restarted successfully",
        }
    ))


async def fake_alerting_agent(event: Event):
    """Simulates AlertingAgent receiving ALERT_SENT."""
    summary = event.data
    log("AlertingAgent", f"Sending Slack notification...")
    log("AlertingAgent", f"  Service  : {summary.get('service')}")
    log("AlertingAgent", f"  Status   : {summary.get('status')}")
    log("AlertingAgent", f"  Cause    : {summary.get('root_cause')}")
    log("AlertingAgent", f"  Action   : {summary.get('action_taken')}")


# ============================================================
# Setup
# ============================================================

bus      = EventBus()
state    = StateManager()
registry = AgentRegistry()

orchestrator = Orchestrator(
    event_bus=bus,
    state_manager=state,
    agent_registry=registry,
    auto_remediate=True,
    max_retries=3,
)


# ============================================================
# Main Test
# ============================================================

async def run_test():

    separator("STEP 0 — System Startup")

    # Start Orchestrator (subscribes to Events)
    orchestrator.start()
    log("System", "Orchestrator started")

    # Register fake agents in registry
    registry.register("monitoring_agent",   "monitoring_agent-001")
    registry.register("knowledge_agent",    "knowledge_agent-001")
    registry.register("self_healing_agent", "self_healing_agent-001")
    registry.register("alerting_agent",     "alerting_agent-001")

    registry.set_status("monitoring_agent",   AgentStatus.RUNNING)
    registry.set_status("knowledge_agent",    AgentStatus.RUNNING)
    registry.set_status("self_healing_agent", AgentStatus.RUNNING)
    registry.set_status("alerting_agent",     AgentStatus.RUNNING)

    log("System", f"Registry: {registry}")

    # Subscribe fake agent handlers to Events
    bus.subscribe(EventType.INVESTIGATION_STARTED,  fake_knowledge_agent)
    bus.subscribe(EventType.REMEDIATION_STARTED,    fake_self_healing_agent)
    bus.subscribe(EventType.ALERT_SENT,             fake_alerting_agent)

    # Verify all agents
    missing = orchestrator.verify_agents()
    assert missing == [], f"Missing agents: {missing}"
    log("System", "All agents verified ✅")

    # --------------------------------------------------------
    separator("STEP 1 — MonitoringAgent Detects Anomaly")

    incident = state.create_incident(
        service="auth-api",
        severity=IncidentSeverity.HIGH,
        description="Error rate spiked to 45%",
        metrics={"error_rate": 0.45, "cpu": 0.91, "memory": 0.98},
        logs=[
            "ERROR: connection timeout after 30s",
            "ERROR: OOM killed — process exceeded memory limit",
            "WARN:  pod restarting (exit code 137)",
        ]
    )
    log("MonitoringAgent", f"Incident created: {incident}")
    assert incident.status == IncidentStatus.DETECTED

    # --------------------------------------------------------
    separator("STEP 2 — Publish INCIDENT_CREATED")

    await bus.publish(Event(
        type=EventType.INCIDENT_CREATED,
        source="monitoring_agent",
        incident_id=incident.incident_id,
        data={"service": incident.service}
    ))

    # --------------------------------------------------------
    separator("STEP 3 — Verify Final State")

    # Fetch updated incident from StateManager
    final = state.get_incident(incident.incident_id)
    log("StateManager", f"Final incident state: {final}")
    log("StateManager", f"Status: {final.status}")

    assert final.status == IncidentStatus.RESOLVED, (
        f"Expected RESOLVED, got {final.status}"
    )

    # Check context was cleaned up
    active = orchestrator.get_active_incidents()
    assert incident.incident_id not in active, "Context should be cleaned up"

    # Check EventBus history
    history = bus.get_history(incident_id=incident.incident_id)
    log("EventBus", f"Total events for this incident: {len(history)}")
    for e in history:
        log("EventBus", f"  -> {e.type} (from {e.source})")

    # Stats
    stats = state.get_stats()
    log("StateManager", f"Stats: {stats}")

    separator("ALL TESTS PASSED ✅")


# ============================================================
# Run
# ============================================================

if __name__ == "__main__":
    asyncio.run(run_test())