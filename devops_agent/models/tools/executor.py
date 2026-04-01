import json
from typing import Dict
from devops_agent.models.tools.base_tool import BaseTool
from devops_agent.models.tools.bash_tool import BashTool
from devops_agent.models.tools.file_tool import ReadFileTool, WriteFileTool, ListDirectoryTool
from devops_agent.models.tools.git_tool import GitStatusTool, GitDiffTool, GitCommitTool


class ToolExecutor:
    """
    Registry of all available tools.
    To add a new tool: create it in models/tools/, import it here, add to _register().
    """

    def __init__(self):
        self._tools: Dict[str, BaseTool] = {}
        self._register()

    def _register(self):
        tools = [
            BashTool(),
            ReadFileTool(),
            WriteFileTool(),
            ListDirectoryTool(),
            GitStatusTool(),
            GitDiffTool(),
            GitCommitTool(),
        ]
        for tool in tools:
            self._tools[tool.name] = tool

    def get_schemas(self) -> list:
        """Return all tool schemas for the LLM."""
        return [t.to_groq_schema() for t in self._tools.values()]

    def execute(self, tool_name: str, arguments: dict) -> str:
        """Run a tool by name with given arguments."""
        if tool_name not in self._tools:
            return f"[ERROR] Unknown tool: {tool_name}"
        try:
            return self._tools[tool_name].run(**arguments)
        except Exception as e:
            return f"[ERROR] Tool '{tool_name}' failed: {e}"

    def parse_and_execute(self, tool_call) -> tuple[str, str]:
        """Parse a Groq tool call object and execute it. Returns (tool_name, result)."""
        name = tool_call.function.name
        try:
            args = json.loads(tool_call.function.arguments)
        except json.JSONDecodeError:
            args = {}
        result = self.execute(name, args)
        return name, result