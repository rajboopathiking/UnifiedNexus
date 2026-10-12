import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)) + "/..")

import pytest
from unittest.mock import patch, MagicMock
from fastapi import UploadFile, Request, Response
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware
from pydantic import BaseModel
from unified_nexus import UnifiedNexus, ExposureMode


class SampleModel(BaseModel):
    name: str


def test_auto_method_detection():
    nexus = UnifiedNexus("MethodDetectionApp")

    @nexus.universal_tool("/with-body")
    def post_func(data: SampleModel):
        return {"name": data.name}

    @nexus.universal_tool("/with-query")
    def get_func(query: str = "default"):
        return {"query": query}

    @nexus.universal_tool("/custom-put", methods=["PUT"])
    def put_func(item_id: int):
        return {"id": item_id}

    app = nexus.finalize(mode="api")

    routes_by_path = {r.path: r.methods for r in app.routes if hasattr(r, "methods")}
    assert "POST" in routes_by_path["/with-body"]
    assert "GET" in routes_by_path["/with-query"]
    assert "PUT" in routes_by_path["/custom-put"]


@pytest.mark.asyncio
async def test_async_and_sync_execution():
    nexus = UnifiedNexus("SyncAsyncApp")

    @nexus.universal_tool("/sync-endpoint")
    def sync_endpoint(x: int):
        return {"result": x * 2}

    @nexus.universal_tool("/async-endpoint")
    async def async_endpoint(x: int):
        return {"result": x * 3}

    app = nexus.finalize()

    # Test HTTP execution
    with TestClient(app) as client:
        assert client.get("/sync-endpoint?x=5").json() == {"result": 10}
        assert client.get("/async-endpoint?x=5").json() == {"result": 15}

    # Test FastMCP tool execution
    sync_tool = await nexus.mcp.get_tool("sync_endpoint")
    sync_res = await sync_tool.run({"x": 5})
    assert "10" in str(sync_res)

    async_tool = await nexus.mcp.get_tool("async_endpoint")
    async_res = await async_tool.run({"x": 5})
    assert "15" in str(async_res)


@pytest.mark.asyncio
async def test_llm_friendly_error_formatting():
    nexus = UnifiedNexus("ErrorHintApp")

    @nexus.universal_tool("/divide")
    def divide(a: int, b: int):
        if b == 0:
            raise ZeroDivisionError("division by zero is not allowed")
        return {"val": a / b}

    tool = await nexus.mcp.get_tool("divide")
    err_res = await tool.run({"a": 10, "b": 0})
    result_str = str(err_res)
    assert "error" in result_str
    assert "hint" in result_str
    assert "division by zero" in result_str


def test_custom_middleware_execution():
    nexus = UnifiedNexus("MiddlewareApp")

    class CustomHeaderMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            response = await call_next(request)
            response.headers["X-Custom-Header"] = "UnifiedNexusProtected"
            return response

    nexus.add_middleware(CustomHeaderMiddleware)

    @nexus.universal_tool("/ping")
    def ping():
        return {"pong": True}

    app = nexus.finalize(mode="api")
    with TestClient(app) as client:
        res = client.get("/ping")
        assert res.status_code == 200
        assert res.headers.get("X-Custom-Header") == "UnifiedNexusProtected"


@pytest.mark.asyncio
async def test_http_specific_params_skip_mcp_gracefully():
    nexus = UnifiedNexus("UploadApp")

    @nexus.universal_tool("/upload-file")
    def handle_upload(file: UploadFile):
        return {"filename": file.filename}

    @nexus.universal_tool("/request-raw")
    def handle_request(req: Request):
        return {"client": str(req.client)}

    app = nexus.finalize()

    # Endpoints exist in HTTP
    with TestClient(app) as client:
        assert client.post("/upload-file").status_code == 422 # missing required file body

    # Endpoints safely skipped from FastMCP (cannot serialize Request/UploadFile)
    tools = await nexus.mcp.list_tools()
    tool_names = [t.name for t in tools]
    assert "handle_upload" not in tool_names
    assert "handle_request" not in tool_names


def test_run_helper():
    nexus = UnifiedNexus("RunHelperApp")

    # Mock uvicorn.run
    with patch("uvicorn.run") as mock_uvicorn:
        nexus.run(transport="http", host="127.0.0.1", port=9000)
        mock_uvicorn.assert_called_once()
        assert mock_uvicorn.call_args[1]["port"] == 9000

    # Mock FastMCP stdio run
    with patch.object(nexus.mcp, "run") as mock_mcp_run:
        nexus.run(transport="stdio")
        mock_mcp_run.assert_called_once_with(transport="stdio")
