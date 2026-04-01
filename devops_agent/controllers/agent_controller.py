import json
import sys
from pathlib import Path

# ── Path fix ──────────────────────────────────────────────────────────────────
# agent_controller.py lives in  devops_agent/controllers/
# models/, views/, and config.py live in  devops_agent/
# We need devops_agent/ on sys.path so bare imports work regardless of the
# working directory the user runs `devops` from.
_DEVOPS_AGENT_DIR = Path(__file__).resolve().parent.parent  # …/devops_agent/
if str(_DEVOPS_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(_DEVOPS_AGENT_DIR))
# ─────────────────────────────────────────────────────────────────────────────

from devops_agent.models.ai_model import AIModel
from devops_agent.models.conversation import ConversationModel
from devops_agent.models.tools.executor import ToolExecutor
from devops_agent.views.cli_view import CLIView
import config


class AgentController:
    """
    The agentic loop.

    Flow per task:
      1. User gives a task
      2. LLM responds — either with text (done) or tool calls (keep looping)
      3. Tools execute, results fed back to LLM
      4. Repeat until LLM gives a plain text response or max iterations hit
    """

    def __init__(self):
        self.executor = ToolExecutor()
        self.model = AIModel(tools=self.executor.get_schemas())
        self.conversation = ConversationModel(system_prompt=config.SYSTEM_PROMPT)
        self.view = CLIView()

    def run(self):
        self.view.show_welcome()

        while True:
            user_input = self.view.get_input()

            if not user_input:
                continue

            if user_input.lower() in config.EXIT_COMMANDS:
                self.view.show_goodbye()
                break

            if user_input.lower() == "clear":
                self.conversation.clear()
                self.view.show_info("Conversation cleared.")
                continue

            if user_input.lower() == "tools":
                tool_names = [s["function"]["name"] for s in self.executor.get_schemas()]
                self.view.show_tools_list(tool_names)
                continue

            self._run_task(user_input)

    def _run_task(self, user_input: str):
        """Run the agentic loop for a single task."""
        self.conversation.add_user_message(user_input)

        for iteration in range(1, config.MAX_ITERATIONS + 1):
            self.view.show_thinking()

            try:
                response = self.model.get_response(self.conversation)
            except RuntimeError as e:
                self.view.clear_line()
                self.view.show_error(str(e))
                return

            self.view.clear_line()
            message = response.choices[0].message

            # ── Case 1: LLM wants to call tools ───────────────────────────
            if message.tool_calls:
                # Show any accompanying text first
                if message.content:
                    self.view.show_agent_text(message.content)

                # Record assistant message with tool calls
                self.conversation.add_assistant_message(
                    content=message.content,
                    tool_calls=message.tool_calls,
                )

                # Execute each tool call
                for tool_call in message.tool_calls:
                    tool_name = tool_call.function.name
                    try:
                        args = json.loads(tool_call.function.arguments)
                    except json.JSONDecodeError:
                        args = {}

                    self.view.show_tool_call(tool_name, args)
                    result = self.executor.execute(tool_name, args)
                    self.view.show_tool_result(result)

                    # Feed result back into conversation
                    self.conversation.add_tool_result(
                        tool_call_id=tool_call.id,
                        tool_name=tool_name,
                        result=result,
                    )

                # Loop — let LLM decide next step
                continue

            # ── Case 2: LLM gave a plain text response (task done) ────────
            else:
                if message.content:
                    self.view.show_agent_text(message.content)
                    self.conversation.add_assistant_message(content=message.content)
                self.view.show_task_complete()
                return

        # Hit max iterations
        self.view.show_max_iterations()