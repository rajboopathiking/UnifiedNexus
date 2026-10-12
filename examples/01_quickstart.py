"""
01_quickstart.py — Basic Universal Tool with UnifiedNexus

Demonstrates:
- Creating a unified FastAPI + FastMCP application.
- Auto-detecting HTTP methods (GET for query params, POST for Pydantic models).
- Using docstrings and Pydantic Field descriptions for Swagger and AI agents.
"""

import os
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)) + "/..")

import uvicorn
from pydantic import BaseModel, Field

from unified_nexus import UnifiedNexus

nexus = UnifiedNexus("QuickstartApp", version="1.0.0")

class UserRequest(BaseModel):
    user_id: int = Field(..., description="Unique numerical ID of the user")

# POST endpoint auto-detected because of Pydantic model body
@nexus.universal_tool(path="/user-info")
def get_user(req: UserRequest):
    """
    Fetches user status and clearance level.
    This docstring is used by both Swagger UI and AI Agents automatically.
    """
    return {"id": req.user_id, "status": "Active", "tier": "Gold"}

# GET endpoint auto-detected because of query parameter
@nexus.universal_tool(path="/ping")
def ping(echo: str = "pong"):
    """Simple ping-pong health check."""
    return {"echo": echo}

app = nexus.finalize()

if __name__ == "__main__":
    print("REST Docs  → http://localhost:8000/docs")
    print("MCP Endpoint → http://localhost:8000/mcp")
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
