"""
test_core.py
------------
End-to-end test for the Core Architecture.

Tests:
    1. Models creation
    2. EventBus pub/sub
    3. StateManager
    4. ContextManager
    5. AgentRegistry
    6. Orchestrator full workflow
"""
import sys
import os
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
KNOWLEDGE_AGENT = os.path.join(ROOT, "agents", "knowledge_agent")

sys.path.insert(0, KNOWLEDGE_AGENT)   
sys.path.insert(0, ROOT) 

import asyncio
import logging
from knowledge_core.knowledge_agent_adapter import KnowledgeAgentAdapter
from ingestion.pipeline import run_pipeline
logging.basicConfig(level=logging.INFO)

from core import (
    Orchestrator,
    EventBus,
    Event,
    EventType,
    StateManager,
    ContextManager,
    AgentRegistry,
    Incident,
    Metric,
    Log,
    Solution,
    Severity,
    IncidentStatus,
    AgentStatus,
)


# ============================================================
# Dummy Agents for testing
# ============================================================

class DummyKnowledgeAgent:
    async def investigate(self, context):
        print(f"[DummyKnowledgeAgent] Investigating: {context.incident.incident_id}")
        return Solution(
            incident_id=context.incident.incident_id,
            root_cause="Memory leak detected in auth-api",
            healing_prompt="Restart the deployment",
            confidence=0.90,
            suggested_commands=["kubectl rollout restart deployment/auth-api"],
        )

class DummySelfHealingAgent:
    async def remediate(self, solution):
        print(f"[DummySelfHealingAgent] Remediating: {solution.incident_id}")
        print(f"  Command: {solution.suggested_commands[0]}")

class DummyAlertingAgent:
    async def send(self, incident_id, title, message):
        print(f"[DummyAlertingAgent] Alert sent: {title}")
        print(f"  Message: {message}")


# ============================================================
# Tests
# ============================================================

def test_models():
    print("\n--- Test 1: Models ---")

    incident = Incident(
        service="auth-api",
        severity=Severity.HIGH,
        description="API error rate > 40%",
    )
    print(f"  Incident: {incident}")

    metric = Metric(
        name="error_rate",
        value=0.45,
        unit="%",
        service="auth-api",
    )
    print(f"  Metric: {metric}")

    log = Log(
        message="Connection timeout after 30s",
        level="ERROR",
        service="auth-api",
    )
    print(f"  Log: {log}")

    assert incident.status == IncidentStatus.OPEN
    print("  PASSED")


def test_event_bus():
    print("\n--- Test 2: EventBus ---")

    bus = EventBus()
    received = []

    async def handler(event):
        received.append(event)
        print(f"  Handler received: {event}")

    bus.subscribe(EventType.INCIDENT_CREATED, handler)

    async def run():
        await bus.publish(Event(
            type=EventType.INCIDENT_CREATED,
            source="test",
            data={"service": "auth-api"},
        ))

    asyncio.run(run())
    assert len(received) == 1
    print("  PASSED")


def test_state_manager():
    print("\n--- Test 3: StateManager ---")

    state = StateManager()

    incident = Incident(
        service="auth-api",
        severity=Severity.HIGH,
        description="API error rate > 40%",
    )

    state.add_incident(incident)
    state.update_incident_status(incident.incident_id, IncidentStatus.INVESTIGATING)

    retrieved = state.get_incident(incident.incident_id)
    assert retrieved.status == IncidentStatus.INVESTIGATING

    state.set_agent_status("knowledge_agent", AgentStatus.RUNNING)
    assert state.get_agent_status("knowledge_agent") == AgentStatus.RUNNING

    print(f"  Summary: {state.summary()}")
    print("  PASSED")


def test_context_manager():
    print("\n--- Test 4: ContextManager ---")

    ctx_manager = ContextManager()

    incident = Incident(
        service="auth-api",
        severity=Severity.HIGH,
        description="API error rate > 40%",
    )

    ctx = ctx_manager.create_context(incident)

    ctx_manager.add_metrics(incident.incident_id, [
        Metric(name="error_rate", value=0.45, unit="%", service="auth-api"),
    ])
    ctx_manager.add_logs(incident.incident_id, [
        Log(message="Connection timeout", level="ERROR", service="auth-api"),
    ])

    context = ctx_manager.get_context(incident.incident_id)
    assert len(context.metrics) == 1
    assert len(context.logs) == 1

    print(f"  Context text:\n{context.to_text()}")
    print("  PASSED")


def test_agent_registry():
    print("\n--- Test 5: AgentRegistry ---")

    registry = AgentRegistry()

    dummy = DummyKnowledgeAgent()
    registry.register("knowledge_agent", dummy)
    registry.update_status("knowledge_agent", AgentStatus.RUNNING)

    assert registry.is_registered("knowledge_agent")
    assert registry.get_status("knowledge_agent") == AgentStatus.RUNNING

    print(f"  Summary: {registry.summary()}")
    print("  PASSED")


def test_orchestrator():
    print("\n--- Test 6: Orchestrator Full Workflow ---")

    async def run():
        #setup: populate Qdrant 
        
        run_pipeline()
        print("[SETUP] Qdrant populated\n")
        orchestrator = Orchestrator()

        orchestrator.register_agent("knowledge_agent", KnowledgeAgentAdapter())
        # orchestrator.register_agent("knowledge_agent", DummyKnowledgeAgent())
        orchestrator.register_agent("self_healing_agent", DummySelfHealingAgent())
        orchestrator.register_agent("alerting_agent", DummyAlertingAgent())

        await orchestrator.start()

        incident = Incident(
            service="auth-api",
            severity=Severity.HIGH,
            description="API error rate > 40%",
        )

        await orchestrator.handle_incident(incident)

        # give async handlers time to run
        await asyncio.sleep(0.1)

        print(f"  Summary: {orchestrator.summary()}")

    asyncio.run(run())
    print("  PASSED")


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    print("=" * 50)
    print("  Core Architecture — Test Suite")
    print("=" * 50)

    test_models()
    test_event_bus()
    test_state_manager()
    test_context_manager()
    test_agent_registry()
    test_orchestrator()

    print("\n" + "=" * 50)
    print("  All tests passed")
    print("=" * 50)