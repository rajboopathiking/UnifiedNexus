import os
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)) + "/..")

from contextlib import asynccontextmanager

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient

from unified_nexus import UnifiedNexus


def test_attach_to_existing_fastapi_app():
    # 1. Existing FastAPI microservice app
    microservice_app = FastAPI(title="OrderService", version="2.0.0")

    @microservice_app.get("/native-health")
    def native_health():
        return {"status": "ok", "service": "OrderService"}

    # 2. Existing APIRouter in the microservice
    billing_router = APIRouter()

    @billing_router.get("/invoices")
    def list_invoices():
        return {"invoices": ["inv-001", "inv-002"]}


    # 3. UnifiedNexus instance with tools
    nexus = UnifiedNexus("OrderNexus")

    @nexus.universal_tool(path="/orders", expose="both")
    def list_orders():
        return {"orders": ["ord-100"]}

    @nexus.api_only(path="/admin-reset")
    def admin_reset():
        return {"reset": True}

    @nexus.mcp_only
    def agent_refund(order_id: str):
        return {"refunded": order_id}

    # Register router to expose both as REST and MCP
    nexus.include_router(billing_router, prefix="/billing", expose="both")

    # 4. Mount/attach directly to the existing microservice with API version prefix
    nexus.attach_to(microservice_app, api_prefix="/api/v1")

    with TestClient(microservice_app) as client:
        # Existing native routes are untouched
        res_native = client.get("/native-health")
        assert res_native.status_code == 200
        assert res_native.json()["service"] == "OrderService"

        # Universal tool accessible under /api/v1
        res_orders = client.get("/api/v1/orders")
        assert res_orders.status_code == 200
        assert res_orders.json() == {"orders": ["ord-100"]}

        # API-only tool accessible under /api/v1
        res_admin = client.get("/api/v1/admin-reset")
        assert res_admin.status_code == 200

        # Router routes accessible under /api/v1/billing/invoices
        res_billing = client.get("/api/v1/billing/invoices")
        assert res_billing.status_code == 200

        # MCP-only tool is NOT exposed as an HTTP route
        res_agent = client.get("/api/v1/agent_refund")
        assert res_agent.status_code == 404

        # FastMCP endpoint is mounted and active
        res_mcp = client.get("/mcp")
        assert res_mcp.status_code in (400, 406)


@pytest.mark.asyncio
async def test_mcp_tools_on_attached_app():
    app = FastAPI(title="CatalogService")
    nexus = UnifiedNexus("CatalogNexus")

    @nexus.universal_tool("/products", expose="both")
    def get_products():
        return {"products": []}

    @nexus.api_only("/internal-sync")
    def sync_data():
        return {"synced": True}

    @nexus.mcp_only
    def query_recommendations(category: str):
        return {"category": category, "recs": ["item1"]}

    nexus.attach_to(app)

    tools = await nexus.mcp.list_tools()
    tool_names = [t.name for t in tools]
    assert "get_products" in tool_names
    assert "query_recommendations" in tool_names
    assert "sync_data" not in tool_names


def test_lifespan_preservation_on_attach():
    db_state = {"connected": False}

    @asynccontextmanager
    async def database_lifespan(app: FastAPI):
        db_state["connected"] = True
        yield
        db_state["connected"] = False

    app = FastAPI(title="UserService", lifespan=database_lifespan)

    nexus = UnifiedNexus("UserNexus")

    @nexus.universal_tool("/users")
    def get_users():
        return {"users": ["Alice"], "db": db_state["connected"]}

    nexus.attach_to(app)

    with TestClient(app) as client:
        # Both user's existing DB lifespan and FastMCP lifespan ran
        assert db_state["connected"] is True
        res = client.get("/users")
        assert res.status_code == 200
        assert res.json()["db"] is True

        # MCP is active
        assert client.get("/mcp").status_code in (400, 406)

    # DB disconnected on shutdown
    assert db_state["connected"] is False


def test_unsupported_depends_rejected_in_mcp():
    from unified_nexus import SecurityConfigurationError

    def get_db_session():
        return "mock_postgres_session"

    nexus = UnifiedNexus("DepApp")

    # 1. Attempting to expose FastAPI Depends to MCP raises SecurityConfigurationError
    with pytest.raises(SecurityConfigurationError) as exc_info:
        @nexus.universal_tool(path="/profile", expose="both")
        async def get_profile_insecure(db_conn: str = Depends(get_db_session)):
            return {"db": db_conn}

    assert "FastAPI dependency injection" in str(exc_info.value)

    # 2. Endpoints with Depends are safely supported via expose='api' or @nexus.api_only
    @nexus.api_only(path="/profile-api")
    def get_profile_safe(db_conn: str = Depends(get_db_session)):
        return {"db": db_conn}

    app = nexus.finalize(mode="api")
    with TestClient(app) as client:
        res = client.get("/profile-api")
        assert res.status_code == 200
        assert res.json() == {"db": "mock_postgres_session"}


def test_constructor_app_binding():
    existing_app = FastAPI(title="ExistingApp")
    nexus = UnifiedNexus("BoundNexus", app=existing_app)

    @nexus.universal_tool("/ping")
    def ping():
        return {"pong": True}

    app = nexus.finalize(api_prefix="/v2")
    assert app is existing_app

    with TestClient(existing_app) as client:
        assert client.get("/v2/ping").status_code == 200
        assert client.get("/mcp").status_code in (400, 406)


def test_combine_lifespan_helper():
    events = []

    @asynccontextmanager
    async def custom_lifespan(app: FastAPI):
        events.append("custom_start")
        yield
        events.append("custom_stop")

    nexus = UnifiedNexus("LifespanApp")
    app = FastAPI(lifespan=nexus.get_lifespan(custom_lifespan))
    nexus.attach_to(app)

    with TestClient(app) as client:
        assert "custom_start" in events
        assert client.get("/mcp").status_code in (400, 406)

    assert "custom_stop" in events


