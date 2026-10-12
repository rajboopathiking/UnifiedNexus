import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)) + "/..")

import uvicorn
from pydantic import BaseModel, Field
from unified_nexus import UnifiedNexus

# Application-level exposure control: "both" (default), "api", or "mcp"
nexus = UnifiedNexus("My Production App", expose="both")

class UserLookup(BaseModel):
    user_id: int = Field(..., description="Numerical ID of the employee")

# 1. Exposed to BOTH REST API & MCP
@nexus.universal_tool(path="/user-status", expose="both")
def check_user_status(payload: UserLookup):
    """Retrieves the active status of a user."""
    return {"user_id": payload.user_id, "status": "Active", "role": "Admin"}

# 2. Exposed ONLY to REST API (e.g. web hooks, monitoring)
@nexus.api_only(path="/health")
def health_check():
    """Health check endpoint for web clients and monitoring."""
    return {"status": "healthy"}

# 3. Exposed ONLY to MCP AI Agents (e.g. LLM-specific actions)
@nexus.mcp_only
def internal_agent_planner(query: str):
    """Internal planning tool for AI agents."""
    return {"plan": f"Execute task for: {query}"}

app = nexus.finalize()

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")