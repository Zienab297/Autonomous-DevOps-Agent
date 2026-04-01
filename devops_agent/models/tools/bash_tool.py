import subprocess
from devops_agent.models.tools.base_tool import BaseTool


class BashTool(BaseTool):

    @property
    def name(self) -> str:
        return "run_command"

    @property
    def description(self) -> str:
        return (
            "Run a shell command on the local machine and return its output. "
            "Use this to execute scripts, check system state, install packages, etc."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute.",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default: 30).",
                    "default": 30,
                },
            },
            "required": ["command"],
        }

    def run(self, command: str, timeout: int = 30) -> str:
        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            output = result.stdout.strip()
            error = result.stderr.strip()

            if result.returncode != 0:
                return f"[exit code {result.returncode}]\nSTDOUT: {output}\nSTDERR: {error}"
            return output or "[command completed with no output]"

        except subprocess.TimeoutExpired:
            return f"[ERROR] Command timed out after {timeout}s"
        except Exception as e:
            return f"[ERROR] {e}"