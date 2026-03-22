import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from controllers.agent_controller import AgentController
from models.ai_model import AIModel
from models.conversation import ConversationModel
from models.tools.executor import ToolExecutor


class DevOpsAgent:
    """
    SDK interface for the Autonomous DevOps Agent.
    Use this to embed the agent in other scripts or projects.
    """

    def __init__(self):
        self.executor = ToolExecutor()
        self.model = AIModel(tools=self.executor.get_schemas())
        self.conversation = ConversationModel()

    def run_cli(self):
        """Launch the interactive CLI."""
        AgentController().run()

    def run_task(self, task: str) -> str:
        """
        Run a single task programmatically and return the result.
        No CLI loop — just input a task, get a response back.
        """
        from controllers.agent_controller import AgentController
        agent = AgentController()
        agent.conversation.add_user_message(task)
        agent._run_task(task)
        history = agent.conversation.last_messages(1)
        return history[0].content if history else ""




"""
this is an activation for the SDK when run directly. It demonstrates how to use the DevOpsAgent class.
You can run this file to start the CLI or to execute a single task programmatically.

from devops_agent_sdk import DevOpsAgent

agent = DevOpsAgent()

# Option 1 — interactive CLI
agent.run_cli()

# Option 2 — programmatic single task
result = agent.run_task("check git status and summarize changes")
print(result)
"""