"""
02_exposure_modes.py — Exposure Controls (API only, MCP only, or Both)

Demonstrates:
- Controlling exposure at the application level (both, api, mcp).
- Controlling exposure at the individual tool level.
- Ergonomic shortcut decorators: @nexus.api_only and @nexus.mcp_only.
- Overriding exposure mode dynamically at finalize() time.
"""

import os
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)) + "/..")

import uvicorn
from pydantic import BaseModel, Field

from unified_nexus import UnifiedNexus

# App-level exposure mode: "both" (default), "api", or "mcp"
nexus = UnifiedNexus("ExposureApp", expose="both")

class SearchQuery(BaseModel):
    query: str = Field(..., description="Query string to search for")

# 1. Exposed to BOTH REST API & FastMCP
@nexus.universal_tool(path="/search", expose="both")
def search_catalog(payload: SearchQuery):
    """Searches product catalog. Available via REST and MCP."""
    return {"query": payload.query, "matches": ["item_1", "item_2"]}

# 2. Exposed ONLY to REST API (hidden from AI agents)
@nexus.api_only(path="/metrics")
def internal_metrics():
    """System metrics for Prometheus/monitoring dashboards."""
    return {"cpu_percent": 14.2, "memory_mb": 512}

# 3. Exposed ONLY to MCP (hidden from REST API routes)
@nexus.mcp_only
def agent_reasoning_step(context: str):
    """Internal step-by-step reasoning helper used only by AI agents."""
    return {"status": "analyzed", "context_length": len(context)}

# Build the app — can optionally override exposure mode:
# app = nexus.finalize(expose="api")  # run in API-only mode
# app = nexus.finalize(expose="mcp")  # run in MCP-only mode
app = nexus.finalize(expose="both")   # run both interfaces

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
