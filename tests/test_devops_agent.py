"""
DevOpsAgent Integration Tests
==============================
Tests the CLI agent bridge (agent.py) against the core pipeline.

Covers:
    - Initialization
    - Lifecycle (start/stop)
    - Registry integration
    - Event subscription
    - handle_event() — success and failure paths
    - _run_agentic_task() — with mocked LLM
    - Full pipeline: REMEDIATION_STARTED → REMEDIATION_COMPLETE
    - Full pipeline: REMEDIATION_STARTED → REMEDIATION_FAILED

Run with:
    pytest tests/test_devops_agent.py -v

NOTE: These tests mock the Groq API — no real API key needed.
"""

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from types import SimpleNamespace

from core.event_bus import EventBus, Event, EventType
from core.agent_registery import AgentRegistry
from core.base_agent import AgentState
from core.models import AgentStatus
from devops_agent.agent import DevOpsAgent


# ============================================================
# Helpers — Mock LLM responses
# ============================================================

def make_tool_call(name: str, arguments: dict, call_id: str = "call-001"):
    """Build a mock tool call object matching Groq's response shape."""
    tool_call = MagicMock()
    tool_call.id = call_id
    tool_call.function.name = name
    tool_call.function.arguments = json.dumps(arguments)
    return tool_call


def make_llm_response(content: str = None, tool_calls: list = None):
    """Build a mock Groq API response."""
    message = MagicMock()
    message.content = content
    message.tool_calls = tool_calls or []

    choice = MagicMock()
    choice.message = message

    response = MagicMock()
    response.choices = [choice]
    return response


def make_final_response(content: str = "Task complete. Service restarted successfully."):
    """LLM response with no tool calls — signals task done."""
    return make_llm_response(content=content, tool_calls=[])


def make_tool_response(tool_name: str, args: dict):
    """LLM response that calls a tool."""
    return make_llm_response(
        content=None,
        tool_calls=[make_tool_call(tool_name, args)],
    )


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
def agent(bus, registry):
    return DevOpsAgent(event_bus=bus, registry=registry)


@pytest.fixture
def make_remediation_event():
    def _make(
        incident_id="INC-TEST01",
        service="auth-api",
        action="restart_service",
    ):
        return Event(
            type=EventType.REMEDIATION_STARTED,
            source="orchestrator",
            incident_id=incident_id,
            data={
                "service": service,
                "recommended_action": action,
            },
        )
    return _make


# ============================================================
# 1. Initialization
# ============================================================

class TestDevOpsAgentInit:

    def test_agent_name_is_self_healing_agent(self, agent):
        assert agent.name == "self_healing_agent"

    def test_agent_has_executor(self, agent):
        assert agent.executor is not None

    def test_agent_has_model(self, agent):
        assert agent.model is not None

    def test_agent_initial_state_is_idle(self, agent):
        assert agent.state == AgentState.IDLE
        assert not agent.is_running

    def test_agent_receives_bus_and_registry(self, agent, bus, registry):
        assert agent._bus is bus
        assert agent._registry is registry


# ============================================================
# 2. Lifecycle — start / stop
# ============================================================

class TestDevOpsAgentLifecycle:

    @pytest.mark.asyncio
    async def test_agent_starts_and_is_running(self, agent):
        await agent.start()

        assert agent.state == AgentState.RUNNING
        assert agent.is_running

        await agent.stop()

    @pytest.mark.asyncio
    async def test_agent_registers_in_registry_on_start(self, agent, registry):
        await agent.start()

        assert registry.is_registered("self_healing_agent")

        await agent.stop()

    @pytest.mark.asyncio
    async def test_agent_status_is_running_after_start(self, agent, registry):
        await agent.start()

        status = registry.get_status("self_healing_agent")
        assert status == AgentStatus.RUNNING

        await agent.stop()

    @pytest.mark.asyncio
    async def test_agent_unregisters_on_stop(self, agent, registry):
        await agent.start()
        await agent.stop()

        assert not registry.is_registered("self_healing_agent")

    @pytest.mark.asyncio
    async def test_agent_state_is_stopped_after_stop(self, agent):
        await agent.start()
        await agent.stop()

        assert agent.state == AgentState.STOPPED
        assert not agent.is_running

    @pytest.mark.asyncio
    async def test_agent_subscribes_to_remediation_started(self, agent, bus):
        await agent.start()

        count = bus.get_subscribers_count(EventType.REMEDIATION_STARTED)
        assert count >= 1

        await agent.stop()

    @pytest.mark.asyncio
    async def test_agent_unsubscribes_on_stop(self, agent, bus):
        await agent.start()
        await agent.stop()

        # After stop, no more handlers on the bus
        count = bus.get_subscribers_count(EventType.REMEDIATION_STARTED)
        assert count == 0


# ============================================================
# 3. handle_event() — Success Path
# ============================================================

class TestHandleEventSuccess:

    @pytest.mark.asyncio
    async def test_handle_event_publishes_remediation_complete(
        self, agent, bus, make_remediation_event
    ):
        """When agentic task succeeds, REMEDIATION_COMPLETE is published."""
        completed_events = []

        async def capture(event):
            completed_events.append(event)

        bus.subscribe(EventType.REMEDIATION_COMPLETE, capture)

        # Mock the agentic task to return immediately
        agent._run_agentic_task = AsyncMock(
            return_value="Service restarted successfully."
        )

        await agent.start()
        await agent.handle_event(make_remediation_event())

        assert len(completed_events) == 1
        event = completed_events[0]
        assert event.type == EventType.REMEDIATION_COMPLETE
        assert event.source == "self_healing_agent"
        assert event.data["success"] is True
        assert event.data["output"] == "Service restarted successfully."

        await agent.stop()

    @pytest.mark.asyncio
    async def test_handle_event_passes_incident_id(
        self, agent, bus, make_remediation_event
    ):
        """REMEDIATION_COMPLETE carries the correct incident_id."""
        completed_events = []

        async def capture(event):
            completed_events.append(event)

        bus.subscribe(EventType.REMEDIATION_COMPLETE, capture)
        agent._run_agentic_task = AsyncMock(return_value="done")

        await agent.start()
        await agent.handle_event(make_remediation_event(incident_id="INC-XYZ99"))

        assert completed_events[0].incident_id == "INC-XYZ99"

        await agent.stop()

    @pytest.mark.asyncio
    async def test_handle_event_passes_action_in_output(
        self, agent, bus, make_remediation_event
    ):
        """REMEDIATION_COMPLETE includes the action from the event."""
        completed_events = []

        async def capture(event):
            completed_events.append(event)

        bus.subscribe(EventType.REMEDIATION_COMPLETE, capture)
        agent._run_agentic_task = AsyncMock(return_value="done")

        await agent.start()
        await agent.handle_event(
            make_remediation_event(action="rollback_deployment")
        )

        assert completed_events[0].data["action"] == "rollback_deployment"

        await agent.stop()


# ============================================================
# 4. handle_event() — Failure Path
# ============================================================

class TestHandleEventFailure:

    @pytest.mark.asyncio
    async def test_handle_event_publishes_remediation_failed_on_exception(
        self, agent, bus, make_remediation_event
    ):
        """When agentic task raises, REMEDIATION_FAILED is published."""
        failed_events = []

        async def capture(event):
            failed_events.append(event)

        bus.subscribe(EventType.REMEDIATION_FAILED, capture)

        agent._run_agentic_task = AsyncMock(
            side_effect=RuntimeError("Groq API timeout")
        )

        await agent.start()
        await agent.handle_event(make_remediation_event())

        assert len(failed_events) == 1
        event = failed_events[0]
        assert event.type == EventType.REMEDIATION_FAILED
        assert event.data["success"] is False
        assert "Groq API timeout" in event.data["output"]

        await agent.stop()

    @pytest.mark.asyncio
    async def test_handle_event_failure_carries_incident_id(
        self, agent, bus, make_remediation_event
    ):
        """REMEDIATION_FAILED carries the correct incident_id."""
        failed_events = []

        async def capture(event): failed_events.append(event)

        bus.subscribe(EventType.REMEDIATION_FAILED, capture)
        agent._run_agentic_task = AsyncMock(side_effect=RuntimeError("fail"))

        await agent.start()
        await agent.handle_event(make_remediation_event(incident_id="INC-FAIL01"))

        assert failed_events[0].incident_id == "INC-FAIL01"

        await agent.stop()

    @pytest.mark.asyncio
    async def test_handle_event_does_not_raise_on_failure(
        self, agent, bus, make_remediation_event
    ):
        """handle_event() should never raise — failures are published as events."""
        agent._run_agentic_task = AsyncMock(side_effect=RuntimeError("fail"))

        await agent.start()

        try:
            await agent.handle_event(make_remediation_event())
        except Exception as e:
            pytest.fail(f"handle_event() raised unexpectedly: {e}")

        await agent.stop()


# ============================================================
# 5. _run_agentic_task() — With Mocked LLM
# ============================================================

class TestRunAgenticTask:

    @pytest.mark.asyncio
    async def test_returns_llm_text_when_no_tool_calls(self, agent):
        """When LLM returns plain text, task completes and returns it."""
        mock_response = make_final_response("All done. No issues found.")

        with patch.object(agent.model, "get_response", return_value=mock_response):
            result = await agent._run_agentic_task(
                task="Check the service",
                incident_id="INC-001",
            )

        assert result == "All done. No issues found."

    @pytest.mark.asyncio
    async def test_executes_tool_then_returns_final_response(self, agent):
        """
        LLM calls a tool first, then returns a final text response.
        Verifies the full agentic loop: tool call → result → final response.
        """
        tool_response  = make_tool_response("run_command", {"command": "echo hello"})
        final_response = make_final_response("Command executed successfully.")

        call_count = 0

        def mock_get_response(conversation):
            nonlocal call_count
            call_count += 1
            return tool_response if call_count == 1 else final_response

        with patch.object(agent.model, "get_response", side_effect=mock_get_response):
            with patch.object(
                agent.executor, "execute", return_value="hello"
            ):
                result = await agent._run_agentic_task(
                    task="Run echo hello",
                    incident_id="INC-001",
                )

        assert result == "Command executed successfully."
        assert call_count == 2  # Once for tool call, once for final response

    @pytest.mark.asyncio
    async def test_tool_result_is_added_to_conversation(self, agent):
        """Tool results are fed back into the conversation history."""
        tool_response  = make_tool_response("read_file", {"path": "config.py"})
        final_response = make_final_response("File read.")

        call_count = 0
        captured_conversations = []

        def mock_get_response(conversation):
            nonlocal call_count
            call_count += 1
            captured_conversations.append(
                len(conversation.to_api_format())
            )
            return tool_response if call_count == 1 else final_response

        with patch.object(agent.model, "get_response", side_effect=mock_get_response):
            with patch.object(
                agent.executor, "execute", return_value="# config content"
            ):
                await agent._run_agentic_task(
                    task="Read config.py",
                    incident_id="INC-001",
                )

        # Second call should have more messages (tool result was added)
        assert captured_conversations[1] > captured_conversations[0]

    @pytest.mark.asyncio
    async def test_returns_max_iterations_message_when_loop_never_ends(self, agent):
        """If LLM keeps calling tools and never returns text, loop exits gracefully."""
        tool_response = make_tool_response("run_command", {"command": "ls"})

        with patch.object(agent.model, "get_response", return_value=tool_response):
            with patch.object(agent.executor, "execute", return_value="output"):
                result = await agent._run_agentic_task(
                    task="Keep running tools forever",
                    incident_id="INC-001",
                )

        assert result == "[max iterations reached]"

    @pytest.mark.asyncio
    async def test_multiple_tool_calls_in_single_response(self, agent):
        """Handles multiple tool calls returned in a single LLM response."""
        tool_call_1 = make_tool_call("run_command", {"command": "ls"}, "c1")
        tool_call_2 = make_tool_call("git_status", {}, "c2")

        multi_tool_response = make_llm_response(
            content=None,
            tool_calls=[tool_call_1, tool_call_2],
        )
        final_response = make_final_response("Both tools executed.")

        call_count = 0

        def mock_get_response(conversation):
            nonlocal call_count
            call_count += 1
            return multi_tool_response if call_count == 1 else final_response

        executed_tools = []

        def mock_execute(name, args):
            executed_tools.append(name)
            return f"result of {name}"

        with patch.object(agent.model, "get_response", side_effect=mock_get_response):
            with patch.object(agent.executor, "execute", side_effect=mock_execute):
                result = await agent._run_agentic_task(
                    task="Run multiple tools",
                    incident_id="INC-001",
                )

        assert "run_command" in executed_tools
        assert "git_status" in executed_tools
        assert result == "Both tools executed."

    @pytest.mark.asyncio
    async def test_empty_llm_content_returns_no_response(self, agent):
        """If LLM returns no content and no tool calls, returns fallback string."""
        empty_response = make_final_response(content=None)
        empty_response.choices[0].message.content = None

        with patch.object(agent.model, "get_response", return_value=empty_response):
            result = await agent._run_agentic_task(
                task="Do something",
                incident_id="INC-001",
            )

        assert result == "[no response]"


# ============================================================
# 6. Full Pipeline — Agent wired into EventBus
# ============================================================

class TestFullPipeline:

    @pytest.mark.asyncio
    async def test_remediation_started_triggers_handle_event(
        self, agent, bus, make_remediation_event
    ):
        """Publishing REMEDIATION_STARTED on the bus triggers the agent."""
        agent._run_agentic_task = AsyncMock(return_value="Fixed.")

        completed = []
        async def capture(event): completed.append(event)
        bus.subscribe(EventType.REMEDIATION_COMPLETE, capture)

        await agent.start()
        await bus.publish(make_remediation_event(incident_id="INC-PIPE01"))

        assert len(completed) == 1
        assert completed[0].incident_id == "INC-PIPE01"
        assert completed[0].data["success"] is True

        await agent.stop()

    @pytest.mark.asyncio
    async def test_agent_does_not_handle_events_before_start(
        self, agent, bus, make_remediation_event
    ):
        """Before start(), agent is not subscribed — events are ignored."""
        completed = []
        async def capture(event): completed.append(event)
        bus.subscribe(EventType.REMEDIATION_COMPLETE, capture)

        # Do NOT start the agent
        await bus.publish(make_remediation_event())

        assert len(completed) == 0

    @pytest.mark.asyncio
    async def test_agent_does_not_handle_events_after_stop(
        self, agent, bus, make_remediation_event
    ):
        """After stop(), agent unsubscribes — events are ignored."""
        agent._run_agentic_task = AsyncMock(return_value="done")

        completed = []
        async def capture(event): completed.append(event)
        bus.subscribe(EventType.REMEDIATION_COMPLETE, capture)

        await agent.start()
        await agent.stop()

        await bus.publish(make_remediation_event())

        assert len(completed) == 0

    @pytest.mark.asyncio
    async def test_full_remediation_loop_with_tool_execution(
        self, agent, bus, make_remediation_event
    ):
        """
        End-to-end: REMEDIATION_STARTED → tool call → tool result → 
        final LLM response → REMEDIATION_COMPLETE published.
        """
        tool_response  = make_tool_response("run_command", {"command": "kubectl get pods"})
        final_response = make_final_response("Pods are healthy. Issue resolved.")

        call_count = 0

        def mock_get_response(conversation):
            nonlocal call_count
            call_count += 1
            return tool_response if call_count == 1 else final_response

        completed = []
        async def capture(event): completed.append(event)
        bus.subscribe(EventType.REMEDIATION_COMPLETE, capture)

        with patch.object(agent.model, "get_response", side_effect=mock_get_response):
            with patch.object(
                agent.executor, "execute",
                return_value="pod/auth-api-xyz Running"
            ):
                await agent.start()
                await bus.publish(make_remediation_event(incident_id="INC-E2E01"))

        assert len(completed) == 1
        assert completed[0].incident_id == "INC-E2E01"
        assert completed[0].data["success"] is True
        assert "resolved" in completed[0].data["output"].lower()

        await agent.stop()

    @pytest.mark.asyncio
    async def test_orchestrator_receives_complete_event(
        self, agent, bus, make_remediation_event
    ):
        """
        Verify the orchestrator-side handler receives REMEDIATION_COMPLETE
        when the agent finishes successfully.
        """
        orchestrator_received = []

        async def mock_orchestrator_handler(event):
            orchestrator_received.append(event)

        bus.subscribe(EventType.REMEDIATION_COMPLETE, mock_orchestrator_handler)

        agent._run_agentic_task = AsyncMock(
            return_value="Rollback completed successfully."
        )

        await agent.start()
        await bus.publish(make_remediation_event(
            incident_id="INC-ORCH01",
            action="rollback_deployment",
        ))

        assert len(orchestrator_received) == 1
        event = orchestrator_received[0]
        assert event.source == "self_healing_agent"
        assert event.incident_id == "INC-ORCH01"
        assert event.data["action"] == "rollback_deployment"

        await agent.stop()