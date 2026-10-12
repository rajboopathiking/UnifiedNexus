import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)) + "/..")

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel
from unified_nexus import UnifiedNexus, ExposureMode, normalize_exposure


def test_normalize_exposure():
    assert normalize_exposure("both") == ExposureMode.BOTH
    assert normalize_exposure("BOTH") == ExposureMode.BOTH
    assert normalize_exposure("api") == ExposureMode.API
    assert normalize_exposure("api_only") == ExposureMode.API
    assert normalize_exposure("api-only") == ExposureMode.API
    assert normalize_exposure("mcp") == ExposureMode.MCP
    assert normalize_exposure("mcp_only") == ExposureMode.MCP
    assert normalize_exposure("mcp-only") == ExposureMode.MCP
    assert normalize_exposure(ExposureMode.API) == ExposureMode.API
    assert normalize_exposure(None) == ExposureMode.BOTH

    with pytest.raises(ValueError):
        normalize_exposure("invalid_mode")


def test_app_mode_properties():
    nexus = UnifiedNexus("PropApp", expose="api")
    assert nexus.mode == ExposureMode.API
    assert nexus.expose == ExposureMode.API

    nexus.mode = "mcp"
    assert nexus.mode == ExposureMode.MCP
    assert nexus.expose == ExposureMode.MCP

    nexus.expose = "both"
    assert nexus.mode == ExposureMode.BOTH
    assert nexus.expose == ExposureMode.BOTH


def test_app_level_api_only():
    nexus = UnifiedNexus("ApiOnlyApp", mode="api")

    @nexus.universal_tool(path="/data")
    def get_data():
        return {"mode": "api"}

    app = nexus.finalize()
    with TestClient(app) as client:
        # API route is accessible
        res = client.get("/data")
        assert res.status_code == 200
        assert res.json() == {"mode": "api"}

        # REST Docs are accessible
        docs_res = client.get("/docs")
        assert docs_res.status_code == 200

        # MCP is not mounted (404)
        mcp_res = client.get("/mcp")
        assert mcp_res.status_code == 404


def test_app_level_mcp_only():
    nexus = UnifiedNexus("McpOnlyApp", mode="mcp")

    @nexus.universal_tool(path="/data")
    def get_data():
        return {"mode": "mcp"}

    app = nexus.finalize()
    with TestClient(app) as client:
        # REST routes are disabled in MCP-only mode (404)
        res = client.get("/data")
        assert res.status_code == 404

        # REST Docs are disabled (404)
        docs_res = client.get("/docs")
        assert docs_res.status_code == 404

        # MCP is mounted (returns 400 or 406 for non-SSE GET depending on FastMCP version)
        mcp_res = client.get("/mcp")
        assert mcp_res.status_code in (400, 406)


def test_app_level_both():
    nexus = UnifiedNexus("BothApp", mode="both")

    @nexus.universal_tool(path="/data")
    def get_data():
        return {"mode": "both"}

    app = nexus.finalize()
    with TestClient(app) as client:
        # API route is accessible
        res = client.get("/data")
        assert res.status_code == 200
        assert res.json() == {"mode": "both"}

        # REST Docs are accessible
        docs_res = client.get("/docs")
        assert docs_res.status_code == 200

        # MCP is mounted (returns 400 or 406 for non-SSE GET depending on FastMCP version)
        mcp_res = client.get("/mcp")
        assert mcp_res.status_code in (400, 406)


def test_finalize_override_mode():
    nexus = UnifiedNexus("OverrideApp", mode="both")

    @nexus.universal_tool(path="/test")
    def get_test():
        return {"status": "ok"}

    # Override to API only at finalize time
    app_api = nexus.finalize(mode="api")
    with TestClient(app_api) as client:
        assert client.get("/test").status_code == 200
        assert client.get("/mcp").status_code == 404

    # Override to MCP only at finalize time
    app_mcp = nexus.finalize(expose="mcp")
    with TestClient(app_mcp) as client:
        assert client.get("/test").status_code == 404
        assert client.get("/mcp").status_code in (400, 406)



@pytest.mark.asyncio
async def test_tool_level_exposure():
    nexus = UnifiedNexus("ToolLevelApp", mode="both")

    @nexus.universal_tool(path="/tool-api", expose="api")
    def api_tool():
        return {"type": "api"}

    @nexus.universal_tool(expose="mcp")
    def mcp_tool():
        return {"type": "mcp"}

    @nexus.universal_tool(path="/tool-both", expose="both")
    def both_tool():
        return {"type": "both"}

    @nexus.universal_tool(path="/tool-legacy", include_in_mcp=False)
    def legacy_api_tool():
        return {"type": "legacy"}

    app = nexus.finalize()

    # Verify FastMCP tool registrations
    tools = await nexus.mcp.list_tools()
    tool_names = [t.name for t in tools]
    assert "mcp_tool" in tool_names
    assert "both_tool" in tool_names
    assert "api_tool" not in tool_names
    assert "legacy_api_tool" not in tool_names

    # Verify HTTP route registrations
    with TestClient(app) as client:
        assert client.get("/tool-api").status_code == 200
        assert client.get("/tool-both").status_code == 200
        assert client.get("/tool-legacy").status_code == 200
        # mcp_tool was not registered as HTTP route
        assert client.get("/mcp_tool").status_code == 404


@pytest.mark.asyncio
async def test_shortcuts_api_only_and_mcp_only():
    nexus = UnifiedNexus("ShortcutsApp", mode="both")

    @nexus.api_only("/custom-api")
    def custom_api():
        return {"channel": "api"}

    @nexus.mcp_only
    def custom_mcp():
        return {"channel": "mcp"}

    app = nexus.finalize()

    tools = await nexus.mcp.list_tools()
    tool_names = [t.name for t in tools]
    assert "custom_mcp" in tool_names
    assert "custom_api" not in tool_names

    with TestClient(app) as client:
        assert client.get("/custom-api").status_code == 200
        assert client.get("/custom_mcp").status_code == 404


def test_router_exposure():
    from fastapi import APIRouter

    nexus = UnifiedNexus("RouterApp", mode="both")
    router = APIRouter()

    @nexus.universal_tool(path="/item", router=router, expose="api")
    def get_item():
        return {"item": "ok"}

    @nexus.universal_tool(path="/mcp-item", router=router, expose="mcp")
    def get_mcp_item():
        return {"mcp": "ok"}

    nexus.include_router(router, prefix="/prefix")
    app = nexus.finalize()

    with TestClient(app) as client:
        assert client.get("/prefix/item").status_code == 200
        assert client.get("/prefix/mcp-item").status_code == 404

