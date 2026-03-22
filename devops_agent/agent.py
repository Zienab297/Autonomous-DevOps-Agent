import asyncio
import uuid
from core.base_agent import BaseAgent, AgentEvent
from core.event_bus import EventBus, Event, EventType
from core.agent_registery import AgentRegistry, AgentStatus
from models.tools.executor import ToolExecutor
from models.ai_model import AIModel
from models.conversation import ConversationModel
import config


class DevOpsAgent(BaseAgent):
    """
    The CLI DevOps Agent — acts as the self_healing_agent in the core pipeline.

    Receives REMEDIATION_STARTED events from the Orchestrator,
    runs the agentic loop (LLM + tools), and publishes the result back.
    """

    def __init__(self, event_bus: EventBus, registry: AgentRegistry):
        super().__init__(name="self_healing_agent")
        self.bus = event_bus
        self.registry = registry
        self.executor = ToolExecutor()
        self.model = AIModel(tools=self.executor.get_schemas())

    async def start(self) -> None:
        await super().start()

        # Register in the agent registry
        agent_id = f"self_healing_agent-{str(uuid.uuid4())[:8]}"
        self.registry.register(self.name, agent_id)
        self.registry.set_status(self.name, AgentStatus.RUNNING)

        # Subscribe to events from the orchestrator
        self.bus.subscribe(EventType.REMEDIATION_STARTED, self.handle_event)
        self.logger.info("DevOpsAgent subscribed to REMEDIATION_STARTED")

    async def stop(self) -> None:
        await super().stop()
        self.registry.set_status(self.name, AgentStatus.STOPPED)
        self.registry.unregister(self.name)

    async def handle_event(self, event: Event) -> None:
        """
        Triggered by Orchestrator when a fix needs to be executed.
        Runs the agentic loop and publishes success or failure back.
        """
        incident_id = event.incident_id
        service     = event.data.get("service", "unknown")
        action      = event.data.get("recommended_action", "investigate and fix")

        self.logger.info(f"Handling remediation for {incident_id} — {action}")

        try:
            result = await self._run_agentic_task(
                task=f"Service: {service}\nAction: {action}\nIncident: {incident_id}\n"
                     f"Investigate and remediate this incident using available tools.",
                incident_id=incident_id,
            )

            # Publish success
            await self.bus.publish(Event(
                type=EventType.REMEDIATION_COMPLETE,
                source=self.name,
                incident_id=incident_id,
                data={
                    "action":  action,
                    "success": True,
                    "output":  result,
                }
            ))

        except Exception as e:
            self.logger.error(f"Remediation failed: {e}")

            # Publish failure — orchestrator will retry or escalate
            await self.bus.publish(Event(
                type=EventType.REMEDIATION_FAILED,
                source=self.name,
                incident_id=incident_id,
                data={
                    "action":  action,
                    "success": False,
                    "output":  str(e),
                }
            ))

    async def _run_agentic_task(self, task: str, incident_id: str) -> str:
        """Run the full agentic loop for a remediation task."""
        conversation = ConversationModel(system_prompt=config.SYSTEM_PROMPT)
        conversation.add_user_message(task)

        import json
        for _ in range(config.MAX_ITERATIONS):
            response = await asyncio.to_thread(
                self.model.get_response, conversation
            )
            message = response.choices[0].message

            if message.tool_calls:
                conversation.add_assistant_message(
                    content=message.content,
                    tool_calls=message.tool_calls,
                )
                for tool_call in message.tool_calls:
                    name = tool_call.function.name
                    args = json.loads(tool_call.function.arguments or "{}")
                    result = self.executor.execute(name, args)
                    self.logger.info(f"[{name}] {result[:100]}")
                    conversation.add_tool_result(
                        tool_call_id=tool_call.id,
                        tool_name=name,
                        result=result,
                    )
            else:
                return message.content or "[no response]"

        return "[max iterations reached]"