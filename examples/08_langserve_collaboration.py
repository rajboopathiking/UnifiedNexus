"""
08_langserve_collaboration.py — LangServe & UnifiedNexus Collaboration

The Killer Advantage:
- LangServe serves LangChain LCEL chains over HTTP (/invoke, /stream, /batch, playground).
- UnifiedNexus serves tools over Model Context Protocol (MCP) for AI agents (Claude, Cursor).
- When mounted together on ONE FastAPI app:
    1. Web/mobile apps stream LangChain responses via LangServe.
    2. Autonomous AI agents discover and call tools via FastMCP.
    3. MCP tools can invoke LangChain chains in-process.
    4. LangChain agents can use UnifiedNexus tools directly.
    5. Single process, single port, shared database lifespans, zero drift!
"""

import os
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)) + "/..")

import uvicorn
from fastapi import FastAPI

# LangChain & LangServe imports
from langchain_core.runnables import RunnableLambda
from langserve import add_routes
from pydantic import BaseModel, Field

from unified_nexus import UnifiedNexus

# ── 1. Create the Host FastAPI Microservice ───────────────────────────────
app = FastAPI(
    title="HybridGenAIPlatform",
    version="1.0.0",
    description="Unified backend serving LangServe LCEL chains and FastMCP tools side-by-side.",
)

# ── 2. UnifiedNexus Tools ─────────────────────────────────────────────────
nexus = UnifiedNexus("EnterpriseNexus")

class SearchPayload(BaseModel):
    query: str = Field(..., description="Query for vector search")
    top_k: int = Field(3, description="Number of results to retrieve")

# Tool 1: Universal Tool — exposed to REST API and MCP
@nexus.universal_tool(path="/vector-search", expose="both", tags=["Tools"])
def search_knowledge_base(payload: SearchPayload):
    """Semantic vector search across enterprise documentation."""
    return {
        "query": payload.query,
        "results": [
            f"Doc snippet #1 regarding '{payload.query}'",
            f"Doc snippet #2 regarding '{payload.query}'",
        ],
    }

# Tool 2: MCP Tool that invokes an LCEL chain in-process
@nexus.mcp_only
async def agent_summarizer(content: str):
    """Summarizes text using the internal LCEL chain in-process."""
    # Invokes the LangChain chain directly in-process!
    result = await summarizer_chain.ainvoke({"input_text": content})
    return {"summary": result["summary"]}


# ── 3. LangServe LCEL Chain ───────────────────────────────────────────────
# Define a standard LangChain Runnable
def process_summary(data: dict) -> dict:
    text = data.get("input_text", "")
    return {"summary": f"Executive Summary: {text.strip().capitalize()}"}

summarizer_chain = RunnableLambda(process_summary)

# Mount LangServe routes onto FastAPI:
# Provides /summarize/invoke, /summarize/stream, and /summarize/playground
add_routes(
    app,
    summarizer_chain,
    path="/summarize",
)


# ── 4. Mount UnifiedNexus onto the Same FastAPI App ───────────────────────
nexus.attach_to(
    app,
    api_prefix="/api/v1",  # REST routes: /api/v1/vector-search
    mcp_path="/",          # MCP endpoint: /mcp (for Claude Desktop, Cursor, Copilot)
    mode="both",
)

if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("🚀  HYBRID LANGSERVE + UNIFIED NEXUS SERVER ACTIVE")
    print("=" * 60)
    print("🌐  LangServe Playground → http://localhost:8000/summarize/playground/")
    print("🌐  FastAPI REST Docs    → http://localhost:8000/docs")
    print("🤖  FastMCP Endpoint     → http://localhost:8000/mcp")
    print("-" * 60)
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
