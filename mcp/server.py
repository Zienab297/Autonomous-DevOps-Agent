"""
MCP server that exposes the CICDAgent as callable tools.

Run with:
    python -m mcp.server

Or register in Claude Desktop's mcp config:
    {
      "mcpServers": {
        "cicd-agent": {
          "command": "python",
          "args": ["-m", "mcp.server"],
          "env": { "GITHUB_TOKEN": "...", "GITHUB_ORG": "..." }
        }
      }
    }
"""

import asyncio
import json
import os
import sys
import logging

# Ensure project root on path
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

from agents.cicd_agent.cicd_agent import CICDAgent
from providers.cicd.github_provider import GitHubProvider

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mcp.cicd")

# ------------------------------------------------------------------
# Bootstrap agent from environment
# ------------------------------------------------------------------

def _build_agent() -> CICDAgent:
    token = os.environ.get("GITHUB_TOKEN", "")
    org = os.environ.get("GITHUB_ORG", "my-org")
    provider = GitHubProvider(token=token, org=org)
    return CICDAgent(provider=provider)


app = Server("cicd-agent")
agent = _build_agent()

# ------------------------------------------------------------------
# Tool definitions
# ------------------------------------------------------------------

TOOLS = [
    types.Tool(
        name="trigger_pipeline",
        description=(
            "Trigger a CI pipeline for a repository. "
            "Returns the pipeline id, status, and log URL."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository name"},
                "branch": {"type": "string", "default": "main", "description": "Branch to build"},
                "inputs": {"type": "object", "description": "Optional workflow inputs", "default": {}},
                "wait": {"type": "boolean", "default": False, "description": "Block until pipeline finishes"},
            },
            "required": ["repo"],
        },
    ),
    types.Tool(
        name="deploy",
        description=(
            "Deploy a service from a branch or specific version. "
            "Returns the deployment id and status."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "service": {"type": "string", "description": "Service / repo name"},
                "branch": {"type": "string", "default": "main"},
                "version": {"type": "string", "description": "Specific SHA or tag (optional)"},
                "wait": {"type": "boolean", "default": False},
            },
            "required": ["service"],
        },
    ),
    types.Tool(
        name="rollback",
        description="Roll back a service to a specific version or the previous good deployment.",
        inputSchema={
            "type": "object",
            "properties": {
                "service": {"type": "string"},
                "to_version": {"type": "string", "description": "Target version/SHA; omit for previous"},
            },
            "required": ["service"],
        },
    ),
    types.Tool(
        name="get_deployment_logs",
        description="Retrieve logs for a deployment by its id.",
        inputSchema={
            "type": "object",
            "properties": {
                "deployment_id": {"type": "string"},
            },
            "required": ["deployment_id"],
        },
    ),
    types.Tool(
        name="list_deployments",
        description="List recent deployments for a service.",
        inputSchema={
            "type": "object",
            "properties": {
                "service": {"type": "string"},
                "limit": {"type": "integer", "default": 10},
            },
            "required": ["service"],
        },
    ),
]


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return TOOLS


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    logger.info(f"Tool called: {name} args={arguments}")
    try:
        if name == "trigger_pipeline":
            result = await agent.trigger_pipeline(**arguments)
        elif name == "deploy":
            result = await agent.deploy(**arguments)
        elif name == "rollback":
            service = arguments["service"]
            to_version = arguments.get("to_version")
            if to_version:
                result = await agent.rollback(service, to_version)
            else:
                result = await agent.rollback_to_previous(service)
        elif name == "get_deployment_logs":
            lines = await agent.collect_deployment_logs(arguments["deployment_id"])
            result = {"lines": lines}
        elif name == "list_deployments":
            deployments = await agent.list_deployments(
                arguments["service"], arguments.get("limit", 10)
            )
            result = [
                {
                    "id": d.id,
                    "service": d.service,
                    "version": d.version,
                    "status": d.status.value,
                    "deployed_at": d.deployed_at.isoformat(),
                }
                for d in deployments
            ]
        else:
            raise ValueError(f"Unknown tool: {name}")

        return [types.TextContent(type="text", text=json.dumps(result, default=str))]

    except Exception as exc:
        logger.error(f"Tool {name} failed: {exc}", exc_info=True)
        return [types.TextContent(type="text", text=json.dumps({"error": str(exc)}))]


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())