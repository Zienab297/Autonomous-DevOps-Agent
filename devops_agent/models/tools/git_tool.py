import subprocess
from models.tools.base_tool import BaseTool


def _run_git(command: str) -> str:
    result = subprocess.run(
        f"git {command}",
        shell=True,
        capture_output=True,
        text=True,
    )
    out = result.stdout.strip()
    err = result.stderr.strip()
    if result.returncode != 0:
        return f"[git error]\n{err or out}"
    return out or "[git command completed]"


class GitStatusTool(BaseTool):

    @property
    def name(self) -> str:
        return "git_status"

    @property
    def description(self) -> str:
        return "Check the current git status of the repository — branch, staged, unstaged, untracked files."

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}, "required": []}

    def run(self) -> str:
        branch = _run_git("branch --show-current")
        status = _run_git("status --short")
        log = _run_git("log --oneline -5")
        return f"Branch: {branch}\n\nStatus:\n{status or '(clean)'}\n\nLast 5 commits:\n{log}"


class GitDiffTool(BaseTool):

    @property
    def name(self) -> str:
        return "git_diff"

    @property
    def description(self) -> str:
        return "Show the diff of uncommitted changes in the repository."

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "file": {
                    "type": "string",
                    "description": "Optional specific file to diff.",
                }
            },
            "required": [],
        }

    def run(self, file: str = "") -> str:
        return _run_git(f"diff {file}".strip())


class GitCommitTool(BaseTool):

    @property
    def name(self) -> str:
        return "git_commit"

    @property
    def description(self) -> str:
        return "Stage all changes and create a git commit with a message."

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "The commit message.",
                }
            },
            "required": ["message"],
        }

    def run(self, message: str) -> str:
        _run_git("add -A")
        return _run_git(f'commit -m "{message}"')