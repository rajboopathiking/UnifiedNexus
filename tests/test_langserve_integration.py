import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)) + "/..")

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Skip test cleanly if langserve or langchain_core is not installed
langserve = pytest.importorskip("langserve", reason="langserve is required for this test")
langchain_core = pytest.importorskip("langchain_core", reason="langchain_core is required for this test")

from langserve import add_routes
from langchain_core.runnables import RunnableLambda
from pydantic import BaseModel
from unified_nexus import UnifiedNexus


class SearchQuery(BaseModel):
    query: str


@pytest.mark.asyncio
async def test_langserve_and_unified_nexus_collaboration():
    app = FastAPI(title="LangServeNexusApp")

    # 1. LangChain Runnable Chain
    def summarize(data: dict) -> dict:
        text = data.get("input_text", "")
        return {"summary": f"Summarized: {text.upper()}"}

    chain = RunnableLambda(summarize)
    add_routes(app, chain, path="/summarize")

    # 2. UnifiedNexus tools
    nexus = UnifiedNexus("HybridApp")

    @nexus.universal_tool(path="/search", expose="both")
    def search_kb(req: SearchQuery):
        return {"results": [f"Result for {req.query}"]}

    @nexus.mcp_only
    async def mcp_summarize_tool(text: str):
        # In-process execution of LangChain LCEL chain through MCP!
        res = await chain.ainvoke({"input_text": text})
        return {"output": res["summary"]}

    # 3. Mount onto same FastAPI app
    nexus.attach_to(app, api_prefix="/api/v1")

    # 4. Verify LangServe HTTP endpoint
    with TestClient(app) as client:
        ls_res = client.post("/summarize/invoke", json={"input": {"input_text": "hello"}})
        assert ls_res.status_code == 200
        assert ls_res.json()["output"] == {"summary": "Summarized: HELLO"}

        # Verify UnifiedNexus REST endpoint
        nx_res = client.post("/api/v1/search", json={"query": "AI"})
        assert nx_res.status_code == 200
        assert nx_res.json() == {"results": ["Result for AI"]}

        # Verify FastMCP mount
        assert client.get("/mcp").status_code in (400, 406)

    # 5. Verify FastMCP tool call invoking the LangChain chain
    mcp_tool = await nexus.mcp.get_tool("mcp_summarize_tool")
    mcp_res = await mcp_tool.run({"text": "enterprise knowledge"})
    assert "Summarized: ENTERPRISE KNOWLEDGE" in str(mcp_res)
