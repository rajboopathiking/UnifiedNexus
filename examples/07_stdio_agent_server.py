"""
07_stdio_agent_server.py — Running MCP via stdio (Claude Desktop / Cursor CLI)

Demonstrates:
- Using UnifiedNexus to power a local MCP tool server running over stdio.
- Perfect for local AI integrations like Claude Desktop or Cursor CLI.
- Tools defined with @nexus.universal_tool or @nexus.mcp_only are immediately available.
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)) + "/..")

from pydantic import BaseModel, Field
from unified_nexus import UnifiedNexus

nexus = UnifiedNexus("LocalAgentTools")

class CalculatorInput(BaseModel):
    expression: str = Field(..., description="Math expression to evaluate, e.g. '24 * 7'")

@nexus.universal_tool(path="/calculate")
def calculate(inp: CalculatorInput):
    """Safely calculates simple arithmetic expressions."""
    # Basic safe arithmetic
    allowed_chars = set("0123456789+-*/. ()")
    if not set(inp.expression).issubset(allowed_chars):
        raise ValueError("Invalid mathematical characters in expression.")
    return {"expression": inp.expression, "result": eval(inp.expression)}

@nexus.mcp_only
def lookup_file_info(filepath: str):
    """Looks up file existence and size on local machine."""
    exists = os.path.exists(filepath)
    size = os.path.getsize(filepath) if exists else 0
    return {"path": filepath, "exists": exists, "size_bytes": size}

if __name__ == "__main__":
    # To run as standard HTTP: nexus.run(transport="http", port=8000)
    # To run for Claude Desktop / Cursor CLI:
    print("Starting stdio MCP server for AI agent...", file=sys.stderr)
    nexus.run(transport="stdio")
