"""
Tests for UnifiedNexus plugin architecture (NexusPlugin, NexusContext, PluginManager).
Verifies identical plugin interception across REST and MCP transports.
"""

from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient
from fastmcp.exceptions import ToolError

from unified_nexus import (
    ExposureMode,
    NexusContext,
    NexusPlugin,
    UnifiedNexus,
)


class LifecycleTrackingPlugin(NexusPlugin):
    def __init__(self):
        self.events = []
        self.started = False
        self.shutdown_called = False

    async def startup(self, nexus: Any) -> None:
        self.started = True
        self.events.append("startup")

    async def before_execute(self, context: NexusContext, args: Dict[str, Any]) -> None:
        self.events.append(f"before:{context.transport}:{context.tool_name}")

    async def after_execute(self, context: NexusContext, result: Any) -> Any:
        self.events.append(f"after:{context.transport}:{context.tool_name}")
        if isinstance(result, dict):
            result["_plugin_injected"] = True
        return result

    async def on_error(self, context: NexusContext, error: Exception) -> None:
        self.events.append(f"error:{context.transport}:{context.tool_name}:{type(error).__name__}")

    async def shutdown(self) -> None:
        self.shutdown_called = True
        self.events.append("shutdown")


def test_plugin_lifecycle_and_rest_interception():
    plugin = LifecycleTrackingPlugin()
    nexus = UnifiedNexus("PluginApp")
    nexus.add_plugin(plugin)

    @nexus.universal_tool("/greet")
    def greet(name: str = "World"):
        return {"greeting": f"Hello, {name}!"}

    @nexus.universal_tool("/fail")
    def fail_endpoint():
        raise ValueError("Simulated REST endpoint failure")

    app = nexus.finalize(mode="api")

    with TestClient(app) as client:
        # 1. Startup was triggered
        assert plugin.started is True
        assert "startup" in plugin.events

        # 2. REST success path
        res = client.get("/greet?name=Nexus")
        assert res.status_code == 200
        data = res.json()
        assert data["greeting"] == "Hello, Nexus!"
        assert data["_plugin_injected"] is True
        assert "before:rest:greet" in plugin.events
        assert "after:rest:greet" in plugin.events

        # 3. REST error path
        with pytest.raises(ValueError):
            client.get("/fail")
        assert "error:rest:fail_endpoint:ValueError" in plugin.events

    # 4. Shutdown was triggered
    assert plugin.shutdown_called is True
    assert "shutdown" in plugin.events


@pytest.mark.asyncio
async def test_plugin_mcp_interception():
    plugin = LifecycleTrackingPlugin()
    nexus = UnifiedNexus("PluginMcpApp")
    nexus.add_plugin(plugin)

    @nexus.universal_tool("/calculate")
    def calculate(x: int, y: int):
        return {"sum": x + y}

    @nexus.universal_tool("/mcp-fail")
    def mcp_fail():
        raise RuntimeError("Simulated MCP tool failure")

    # Finalize app to run startup
    app = nexus.finalize()
    with TestClient(app):
        assert plugin.started is True

        tool = await nexus.mcp.get_tool("calculate")
        res = await tool.run({"x": 10, "y": 25})
        assert "35" in str(res)
        assert "_plugin_injected" in str(res)
        assert "before:mcp:calculate" in plugin.events
        assert "after:mcp:calculate" in plugin.events

        fail_tool = await nexus.mcp.get_tool("mcp_fail")
        with pytest.raises(ToolError) as exc_fail:
            await fail_tool.run({})
        assert "Simulated MCP tool failure" in str(exc_fail.value)
        assert "error:mcp:mcp_fail:RuntimeError" in plugin.events


def test_multiple_plugins_execution_order():
    call_order = []

    class PluginA(NexusPlugin):
        async def before_execute(self, context: NexusContext, args: dict):
            call_order.append("A_before")

        async def after_execute(self, context: NexusContext, result: Any):
            call_order.append("A_after")
            return result

    class PluginB(NexusPlugin):
        async def before_execute(self, context: NexusContext, args: dict):
            call_order.append("B_before")

        async def after_execute(self, context: NexusContext, result: Any):
            call_order.append("B_after")
            return result

    nexus = UnifiedNexus("OrderApp")
    nexus.add_plugin(PluginA())
    nexus.add_plugin(PluginB())

    @nexus.universal_tool("/ping")
    def ping():
        return {"pong": True}

    app = nexus.finalize(mode="api")
    with TestClient(app) as client:
        client.get("/ping")

    # FIFO execution for before and after
    assert call_order == ["A_before", "B_before", "A_after", "B_after"]
