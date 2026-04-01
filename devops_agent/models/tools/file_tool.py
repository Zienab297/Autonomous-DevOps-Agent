import os
from devops_agent.models.tools.base_tool import BaseTool


class ReadFileTool(BaseTool):

    @property
    def name(self) -> str:
        return "read_file"

    @property
    def description(self) -> str:
        return "Read the contents of a file at a given path."

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute or relative path to the file.",
                }
            },
            "required": ["path"],
        }

    def run(self, path: str) -> str:
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            return content or "[file is empty]"
        except FileNotFoundError:
            return f"[ERROR] File not found: {path}"
        except Exception as e:
            return f"[ERROR] {e}"


class WriteFileTool(BaseTool):

    @property
    def name(self) -> str:
        return "write_file"

    @property
    def description(self) -> str:
        return "Write content to a file. Creates the file if it doesn't exist, overwrites if it does."

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to write.",
                },
                "content": {
                    "type": "string",
                    "description": "Content to write to the file.",
                },
            },
            "required": ["path", "content"],
        }

    def run(self, path: str, content: str) -> str:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            return f"[OK] Written to {path}"
        except Exception as e:
            return f"[ERROR] {e}"


class ListDirectoryTool(BaseTool):

    @property
    def name(self) -> str:
        return "list_directory"

    @property
    def description(self) -> str:
        return "List files and folders in a directory."

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the directory (default: current directory).",
                    "default": ".",
                }
            },
            "required": [],
        }

    def run(self, path: str = ".") -> str:
        try:
            entries = os.listdir(path)
            if not entries:
                return "[directory is empty]"
            return "\n".join(sorted(entries))
        except FileNotFoundError:
            return f"[ERROR] Directory not found: {path}"
        except Exception as e:
            return f"[ERROR] {e}"