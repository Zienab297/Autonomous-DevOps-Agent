"""
Integration Tests — BaseAgent + EventBus + AgentRegistry
==========================================================
Tests the full lifecycle and communication of agents
using the actual core classes.

Run with:
    pytest tests/test_integration.py -v
"""

import asyncio
import pytest
from datetime import datetime
from typing import Any

# ── Core imports ──────────────────────────────────────────────────────────────
from core.base_agent import BaseAgent, AgentEvent, AgentState
from core.agent_registery import AgentRegistry, AgentStatus
from core.event_bus import EventBus, Event, EventType


# ============================================================
# Helpers — Concrete agent implementations for testing
# ============================================================

class EchoAgent(BaseAgent):
    """
    A simple agent that records every event it receives.
    Used to verify event delivery end-to-end.
    """

    def __init__(self, name: str, bus: EventBus, registry: AgentRegistry):
        super().__init__(name=name, event_bus=bus, registry=registry)
        self.received_events: list[Event] = []
        self.setup_called = False
        self.teardown_called = False

    async def _setup(self):
        self.setup_called = True
        self.subscribe(EventType.INCIDENT_CREATED, self.handle_event)
        self.subscribe(EventType.REMEDIATION_STARTED, self.handle_event)

    async def _teardown(self):
        self.teardown_called = True

    async def handle_event(self, event: AgentEvent) -> Any:
        self.received_events.append(event)
        return f"handled:{event.type}"


class BrokenAgent(BaseAgent):
    """An agent that raises during setup — for error handling tests."""

    def __init__(self, bus: EventBus, registry: AgentRegistry):
        super().__init__(name="broken_agent", event_bus=bus, registry=registry)

    async def _setup(self):
        raise RuntimeError("Intentional setup failure")

    async def handle_event(self, event: AgentEvent) -> Any:
        pass


class SlowAgent(BaseAgent):
    """An agent that takes time to process events."""

    def __init__(self, bus: EventBus, registry: AgentRegistry):
        super().__init__(name="slow_agent", event_bus=bus, registry=registry)
        self.processed = []

    async def _setup(self):
        self.subscribe(EventType.METRICS_COLLECTED, self.handle_event)

    async def _teardown(self):
        pass

    async def handle_event(self, event: AgentEvent) -> Any:
        await asyncio.sleep(0.05)  # Simulate async work
        self.processed.append(event)


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
def registry():
    return AgentRegistry()


@pytest.fixture
def echo_agent(bus, registry):
    return EchoAgent(name="echo_agent", bus=bus, registry=registry)


@pytest.fixture
def make_event():
    """Factory for creating test events."""
    def _make(
        event_type=EventType.INCIDENT_CREATED,
        source="test",
        incident_id="INC-TEST01",
        data=None,
    ):
        return Event(
            type=event_type,
            source=source,
            incident_id=incident_id,
            data=data or {"service": "auth-api"},
        )
    return _make


# ============================================================
# 1. BaseAgent — Lifecycle Tests
# ============================================================

class TestBaseAgentLifecycle:

    @pytest.mark.asyncio
    async def test_agent_starts_correctly(self, echo_agent, registry):
        """Agent transitions to RUNNING and registers itself."""
        assert echo_agent.state == AgentState.IDLE
        assert not echo_agent.is_running

        await echo_agent.start()

        assert echo_agent.state == AgentState.RUNNING
        assert echo_agent.is_running
        assert registry.is_running("echo_agent")
        assert echo_agent.setup_called

        await echo_agent.stop()

    @pytest.mark.asyncio
    async def test_agent_stops_correctly(self, echo_agent, registry):
        """Agent transitions to STOPPED and unregisters itself."""
        await echo_agent.start()
        await echo_agent.stop()

        assert echo_agent.state == AgentState.STOPPED
        assert not echo_agent.is_running
        assert not registry.is_registered("echo_agent")
        assert echo_agent.teardown_called

    @pytest.mark.asyncio
    async def test_agent_cannot_start_twice(self, echo_agent):
        """Starting an already-running agent raises RuntimeError."""
        await echo_agent.start()

        with pytest.raises(RuntimeError, match="already running"):
            await echo_agent.start()

        await echo_agent.stop()

    @pytest.mark.asyncio
    async def test_agent_stop_when_not_running_is_safe(self, echo_agent):
        """Stopping an agent that was never started does not raise."""
        await echo_agent.stop()  # Should not raise
        assert echo_agent.state == AgentState.IDLE

    @pytest.mark.asyncio
    async def test_agent_has_unique_id(self, bus, registry):
        """Two agents with the same name get different agent_ids."""
        a1 = EchoAgent(name="echo_agent", bus=bus, registry=registry)
        a2 = EchoAgent(name="echo_agent_2", bus=bus, registry=registry)
        assert a1.agent_id != a2.agent_id

    @pytest.mark.asyncio
    async def test_agent_uptime_increases(self, echo_agent):
        """Uptime is None before start and positive after."""
        assert echo_agent.uptime is None

        await echo_agent.start()
        await asyncio.sleep(0.05)

        assert echo_agent.uptime is not None
        assert echo_agent.uptime > 0

        await echo_agent.stop()

    @pytest.mark.asyncio
    async def test_agent_get_info(self, echo_agent):
        """get_info() returns expected keys."""
        await echo_agent.start()
        info = echo_agent.get_info()

        assert info["name"] == "echo_agent"
        assert info["state"] == "running"
        assert info["uptime_sec"] is not None
        assert info["error"] is None

        await echo_agent.stop()


# ============================================================
# 2. BaseAgent — Error Handling Tests
# ============================================================

class TestBaseAgentErrors:

    @pytest.mark.asyncio
    async def test_agent_state_is_error_on_failed_start(self, bus, registry):
        """Agent enters ERROR state if _setup() raises."""
        agent = BrokenAgent(bus=bus, registry=registry)

        with pytest.raises(RuntimeError, match="Intentional setup failure"):
            await agent.start()

        assert agent.state == AgentState.ERROR
        assert not agent.is_running
        assert registry.get("broken_agent").status == AgentStatus.ERROR

    @pytest.mark.asyncio
    async def test_agent_last_error_is_set(self, bus, registry):
        """last_error stores the exception from a failed start."""
        agent = BrokenAgent(bus=bus, registry=registry)

        with pytest.raises(RuntimeError):
            await agent.start()

        assert agent.last_error is not None
        assert "Intentional setup failure" in str(agent.last_error)


# ============================================================
# 3. EventBus — Core Tests
# ============================================================

class TestEventBus:

    @pytest.mark.asyncio
    async def test_publish_delivers_to_subscriber(self, bus, make_event):
        """A published event reaches the subscribed handler."""
        received = []

        async def handler(event):
            received.append(event)

        bus.subscribe(EventType.INCIDENT_CREATED, handler)
        await bus.publish(make_event(EventType.INCIDENT_CREATED))

        assert len(received) == 1
        assert received[0].type == EventType.INCIDENT_CREATED

    @pytest.mark.asyncio
    async def test_publish_to_multiple_subscribers(self, bus, make_event):
        """Multiple subscribers all receive the same event."""
        results = []

        async def handler_a(event): results.append("A")
        async def handler_b(event): results.append("B")

        bus.subscribe(EventType.INCIDENT_CREATED, handler_a)
        bus.subscribe(EventType.INCIDENT_CREATED, handler_b)
        await bus.publish(make_event(EventType.INCIDENT_CREATED))

        assert "A" in results
        assert "B" in results

    @pytest.mark.asyncio
    async def test_publish_only_reaches_correct_subscriber(self, bus, make_event):
        """An event only reaches handlers subscribed to that event type."""
        received = []

        async def handler(event):
            received.append(event)

        bus.subscribe(EventType.REMEDIATION_STARTED, handler)
        await bus.publish(make_event(EventType.INCIDENT_CREATED))  # Different type

        assert len(received) == 0  # Should not be delivered

    @pytest.mark.asyncio
    async def test_unsubscribe_stops_delivery(self, bus, make_event):
        """After unsubscribe, handler no longer receives events."""
        received = []

        async def handler(event):
            received.append(event)

        bus.subscribe(EventType.INCIDENT_CREATED, handler)
        bus.unsubscribe(EventType.INCIDENT_CREATED, handler)
        await bus.publish(make_event(EventType.INCIDENT_CREATED))

        assert len(received) == 0

    @pytest.mark.asyncio
    async def test_event_history_is_recorded(self, bus, make_event):
        """All published events are stored in history."""
        await bus.publish(make_event(EventType.INCIDENT_CREATED))
        await bus.publish(make_event(EventType.REMEDIATION_STARTED))

        history = bus.get_history()
        assert len(history) == 2

    @pytest.mark.asyncio
    async def test_event_history_filter_by_type(self, bus, make_event):
        """History can be filtered by event type."""
        await bus.publish(make_event(EventType.INCIDENT_CREATED))
        await bus.publish(make_event(EventType.REMEDIATION_STARTED))

        filtered = bus.get_history(event_type=EventType.INCIDENT_CREATED)
        assert len(filtered) == 1
        assert filtered[0].type == EventType.INCIDENT_CREATED

    @pytest.mark.asyncio
    async def test_event_history_filter_by_incident(self, bus, make_event):
        """History can be filtered by incident_id."""
        await bus.publish(make_event(incident_id="INC-001"))
        await bus.publish(make_event(incident_id="INC-002"))

        filtered = bus.get_history(incident_id="INC-001")
        assert len(filtered) == 1
        assert filtered[0].incident_id == "INC-001"

    @pytest.mark.asyncio
    async def test_clear_history(self, bus, make_event):
        """clear_history() empties the event log."""
        await bus.publish(make_event())
        bus.clear_history()
        assert len(bus.get_history()) == 0

    @pytest.mark.asyncio
    async def test_sync_handler_is_supported(self, bus, make_event):
        """EventBus supports both async and sync handlers."""
        received = []

        def sync_handler(event):  # Not async
            received.append(event)

        bus.subscribe(EventType.INCIDENT_CREATED, sync_handler)
        await bus.publish(make_event(EventType.INCIDENT_CREATED))

        assert len(received) == 1

    @pytest.mark.asyncio
    async def test_handler_exception_does_not_stop_other_handlers(self, bus, make_event):
        """If one handler raises, other handlers still execute."""
        results = []

        async def bad_handler(event):
            raise ValueError("Handler failure")

        async def good_handler(event):
            results.append("good")

        bus.subscribe(EventType.INCIDENT_CREATED, bad_handler)
        bus.subscribe(EventType.INCIDENT_CREATED, good_handler)
        await bus.publish(make_event(EventType.INCIDENT_CREATED))

        assert "good" in results  # good_handler still ran


# ============================================================
# 4. AgentRegistry — Tests
# ============================================================

class TestAgentRegistry:

    def test_register_agent(self, registry):
        """Agent can be registered and retrieved."""
        record = registry.register("test_agent", "test_agent-abc123")
        assert record.name == "test_agent"
        assert registry.is_registered("test_agent")

    def test_unregister_agent(self, registry):
        """Agent can be unregistered."""
        registry.register("test_agent", "test_agent-abc123")
        registry.unregister("test_agent")
        assert not registry.is_registered("test_agent")

    def test_set_and_check_status(self, registry):
        """Agent status can be set and queried."""
        registry.register("test_agent", "test_agent-abc123")
        registry.set_status("test_agent", AgentStatus.RUNNING)
        assert registry.is_running("test_agent")

    def test_agent_not_running_after_stop_status(self, registry):
        """Agent is not running after status set to STOPPED."""
        registry.register("test_agent", "test_agent-abc123")
        registry.set_status("test_agent", AgentStatus.RUNNING)
        registry.set_status("test_agent", AgentStatus.STOPPED)
        assert not registry.is_running("test_agent")

    def test_get_all_running(self, registry):
        """get_all_running() returns only RUNNING agents."""
        registry.register("agent_a", "a-001")
        registry.register("agent_b", "b-001")
        registry.set_status("agent_a", AgentStatus.RUNNING)
        registry.set_status("agent_b", AgentStatus.STOPPED)

        running = registry.get_all_running()
        assert len(running) == 1
        assert running[0].name == "agent_a"

    def test_verify_required_agents_all_present(self, registry):
        """verify_required_agents() returns empty list when all are running."""
        registry.register("agent_a", "a-001")
        registry.set_status("agent_a", AgentStatus.RUNNING)

        missing = registry.verify_required_agents(["agent_a"])
        assert missing == []

    def test_verify_required_agents_missing(self, registry):
        """verify_required_agents() returns names of missing agents."""
        missing = registry.verify_required_agents(["missing_agent"])
        assert "missing_agent" in missing

    def test_heartbeat_updates_last_seen(self, registry):
        """heartbeat() updates the last_seen_at timestamp."""
        registry.register("test_agent", "test_agent-abc123")
        before = registry.get("test_agent").last_seen_at

        import time; time.sleep(0.01)
        registry.heartbeat("test_agent")

        after = registry.get("test_agent").last_seen_at
        assert after >= before

    def test_get_stats(self, registry):
        """get_stats() returns correct counts."""
        registry.register("agent_a", "a-001")
        registry.register("agent_b", "b-001")
        registry.set_status("agent_a", AgentStatus.RUNNING)

        stats = registry.get_stats()
        assert stats["total"] == 2
        assert stats["by_status"]["running"] == 1


# ============================================================
# 5. Full Integration — Agent + EventBus + Registry together
# ============================================================

class TestFullIntegration:

    @pytest.mark.asyncio
    async def test_agent_receives_published_event(self, bus, registry, make_event):
        """After start(), an agent receives events published on the bus."""
        agent = EchoAgent(name="echo_agent", bus=bus, registry=registry)
        await agent.start()

        await bus.publish(make_event(EventType.INCIDENT_CREATED))

        assert len(agent.received_events) == 1
        assert agent.received_events[0].type == EventType.INCIDENT_CREATED

        await agent.stop()

    @pytest.mark.asyncio
    async def test_agent_does_not_receive_events_after_stop(self, bus, registry, make_event):
        """After stop(), agent no longer receives events."""
        agent = EchoAgent(name="echo_agent", bus=bus, registry=registry)
        await agent.start()
        await agent.stop()

        await bus.publish(make_event(EventType.INCIDENT_CREATED))

        assert len(agent.received_events) == 0

    @pytest.mark.asyncio
    async def test_multiple_agents_each_receive_their_events(self, bus, registry, make_event):
        """Two agents subscribed to different events each get their own."""
        agent_a = EchoAgent(name="agent_a", bus=bus, registry=registry)
        agent_b = EchoAgent(name="agent_b", bus=bus, registry=registry)

        await agent_a.start()
        await agent_b.start()

        await bus.publish(make_event(EventType.INCIDENT_CREATED))
        await bus.publish(make_event(EventType.REMEDIATION_STARTED))

        # Both subscribed to both events in _setup
        assert len(agent_a.received_events) == 2
        assert len(agent_b.received_events) == 2

        await agent_a.stop()
        await agent_b.stop()

    @pytest.mark.asyncio
    async def test_registry_reflects_running_agents(self, bus, registry, make_event):
        """Registry accurately reflects which agents are running."""
        agent_a = EchoAgent(name="agent_a", bus=bus, registry=registry)
        agent_b = EchoAgent(name="agent_b", bus=bus, registry=registry)

        await agent_a.start()
        await agent_b.start()

        assert len(registry.get_all_running()) == 2

        await agent_a.stop()

        assert len(registry.get_all_running()) == 1
        assert registry.is_running("agent_b")
        assert not registry.is_running("agent_a")

        await agent_b.stop()

    @pytest.mark.asyncio
    async def test_slow_agent_processes_events_async(self, bus, registry, make_event):
        """Async event processing completes correctly even with delays."""
        agent = SlowAgent(bus=bus, registry=registry)
        await agent.start()

        await bus.publish(make_event(EventType.METRICS_COLLECTED))
        await asyncio.sleep(0.1)  # Allow async processing

        assert len(agent.processed) == 1

        await agent.stop()

    @pytest.mark.asyncio
    async def test_event_bus_history_across_multiple_agents(self, bus, registry, make_event):
        """EventBus records all events regardless of how many agents are listening."""
        agent_a = EchoAgent(name="agent_a", bus=bus, registry=registry)
        agent_b = EchoAgent(name="agent_b", bus=bus, registry=registry)

        await agent_a.start()
        await agent_b.start()

        await bus.publish(make_event(EventType.INCIDENT_CREATED, incident_id="INC-001"))
        await bus.publish(make_event(EventType.REMEDIATION_STARTED, incident_id="INC-001"))
        await bus.publish(make_event(EventType.REMEDIATION_COMPLETE, incident_id="INC-001"))

        history = bus.get_history(incident_id="INC-001")
        assert len(history) == 3

        await agent_a.stop()
        await agent_b.stop()

    @pytest.mark.asyncio
    async def test_full_incident_workflow_events(self, bus, registry, make_event):
        """
        Simulate the full incident pipeline event sequence:
        INCIDENT_CREATED -> INVESTIGATION_STARTED -> REMEDIATION_STARTED -> REMEDIATION_COMPLETE
        """
        received_by_type = {}

        async def capture(event):
            received_by_type[event.type] = event

        bus.subscribe(EventType.INCIDENT_CREATED,      capture)
        bus.subscribe(EventType.INVESTIGATION_STARTED, capture)
        bus.subscribe(EventType.REMEDIATION_STARTED,   capture)
        bus.subscribe(EventType.REMEDIATION_COMPLETE,  capture)

        # Simulate the pipeline firing events in sequence
        for event_type in [
            EventType.INCIDENT_CREATED,
            EventType.INVESTIGATION_STARTED,
            EventType.REMEDIATION_STARTED,
            EventType.REMEDIATION_COMPLETE,
        ]:
            await bus.publish(make_event(event_type, incident_id="INC-FLOW01"))

        assert EventType.INCIDENT_CREATED      in received_by_type
        assert EventType.INVESTIGATION_STARTED in received_by_type
        assert EventType.REMEDIATION_STARTED   in received_by_type
        assert EventType.REMEDIATION_COMPLETE  in received_by_type

        # All events should share the same incident_id
        for event in received_by_type.values():
            assert event.incident_id == "INC-FLOW01"