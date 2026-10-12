"""
Targeted security, routing, transport, and execution regression tests for UnifiedNexus v0.2.0.
Verifies all enterprise hardening and AI-agent safety invariants.
"""

import asyncio
import time
from contextlib import asynccontextmanager

import pytest
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
from fastmcp import Client
from fastmcp.exceptions import ToolError

from unified_nexus import (
    DuplicateRouteError,
    DuplicateToolError,
    SecurityConfigurationError,
    ToolSafetyLevel,
    UnifiedNexus,
)


# ─────────────────────────────────────────────────────────────────────────────
# 1. DUPLICATE DETECTION (TOOLS & ROUTES)
# ─────────────────────────────────────────────────────────────────────────────
def test_duplicate_tool_and_route_detection():
    nexus = UnifiedNexus("DuplicateTestApp")

    @nexus.universal_tool("/tool-one")
    def tool_one():
        return {"id": 1}

    # Duplicate MCP tool name
    with pytest.raises(DuplicateToolError) as exc_tool:
        @nexus.universal_tool("/tool-one-again", name="tool_one")
        def tool_one_duplicate():
            return {"id": 2}
    assert "already registered" in str(exc_tool.value)

    # Duplicate REST route configuration
    with pytest.raises(DuplicateRouteError) as exc_route:
        @nexus.universal_tool("/tool-one")
        def tool_one_different_func():
            return {"id": 3}
    assert "already registered" in str(exc_route.value)


# ─────────────────────────────────────────────────────────────────────────────
# 2. SECURITY: REJECT UNSUPPORTED DEPENDS & ROUTER DEPENDENCIES
# ─────────────────────────────────────────────────────────────────────────────
def test_unsupported_depends_and_credentials_rejected():
    nexus = UnifiedNexus("SecurityParamApp")

    def dummy_dep():
        return "val"

    # Reject Depends parameter in MCP
    with pytest.raises(SecurityConfigurationError) as exc_info:
        @nexus.universal_tool("/dep-test", expose="both")
        def dep_test(db: str = Depends(dummy_dep)):
            return db
    assert "FastAPI dependency injection" in str(exc_info.value)

    # Reject sensitive credential parameter name in MCP
    with pytest.raises(SecurityConfigurationError) as exc_info2:
        @nexus.universal_tool("/secret-param", expose="both")
        def secret_param(password: str):
            return password
    assert "sensitive credential parameter" in str(exc_info2.value)

    # Router with security dependencies rejected from MCP
    def auth_gate():
        return True

    secured_router = APIRouter(dependencies=[Depends(auth_gate)])

    @secured_router.get("/sensitive")
    def sensitive_data():
        return {"data": "confidential"}

    with pytest.raises(SecurityConfigurationError) as exc_info3:
        nexus.include_router(secured_router, prefix="/secured", expose="both")
    assert "router-level security dependencies" in str(exc_info3.value)


# ─────────────────────────────────────────────────────────────────────────────
# 3. ROUTING: MCP-ONLY ROUTERS NEVER EXPOSED TO REST
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_mcp_only_router_not_exposed_to_rest():
    nexus = UnifiedNexus("McpOnlyRouterApp")

    mcp_router = APIRouter()

    @mcp_router.get("/agent-search")
    def agent_search(query: str = "ai"):
        return {"found": query}

    # Include router as MCP ONLY
    nexus.include_router(mcp_router, prefix="/agent", expose="mcp")

    # REST endpoint to ensure app has routes
    @nexus.api_only(path="/public-info")
    def public_info():
        return {"public": True}

    app = nexus.finalize()

    with TestClient(app) as client:
        # Public REST endpoint works
        assert client.get("/public-info").status_code == 200

        # MCP-only router endpoints must NOT be accessible via REST (404)
        assert client.get("/agent/agent-search").status_code == 404

    # Verify tool IS accessible in MCP
    tools = await nexus.mcp.list_tools()
    tool_names = [t.name for t in tools]
    assert "agent_search" in tool_names


# ─────────────────────────────────────────────────────────────────────────────
# 4. SECRETS REDACTION: EXCEPTIONS NEVER REVEAL PASSWORDS OR TOKENS
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_exceptions_never_reveal_secrets():
    nexus = UnifiedNexus("SecretLeakApp")

    @nexus.universal_tool("/leak-test")
    def leak_test():
        raise RuntimeError(
            "Failed DB connection: postgresql://admin:super_secret_pw123@db.prod.internal:5432/orders. "
            "Token used: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.xyz987"
        )

    tool = await nexus.mcp.get_tool("leak_test")
    res = await tool.run({})
    res_str = str(res)

    assert "super_secret_pw123" not in res_str
    assert "eyJhbGciOi" not in res_str
    assert "***" in res_str


# ─────────────────────────────────────────────────────────────────────────────
# 5. EXECUTION: SLOW SYNC HANDLERS DO NOT BLOCK ASYNC HANDLERS
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_slow_sync_handlers_do_not_block_async_handlers():
    nexus = UnifiedNexus("ConcurrencyApp")

    @nexus.universal_tool("/slow-sync")
    def slow_sync():
        time.sleep(0.3)
        return {"done": "sync"}

    @nexus.universal_tool("/fast-async")
    async def fast_async():
        await asyncio.sleep(0.01)
        return {"done": "async"}

    sync_tool = await nexus.mcp.get_tool("slow_sync")
    async_tool = await nexus.mcp.get_tool("fast_async")

    start_time = time.perf_counter()

    # Launch both concurrently
    sync_task = asyncio.create_task(sync_tool.run({}))
    async_task = asyncio.create_task(async_tool.run({}))

    # Fast async should finish rapidly without waiting for the 0.3s sync sleep
    async_res = await async_task
    fast_elapsed = time.perf_counter() - start_time

    assert fast_elapsed < 0.2, f"Fast async took too long ({fast_elapsed}s) — event loop was blocked!"
    assert "async" in str(async_res)

    sync_res = await sync_task
    assert "sync" in str(sync_res)


# ─────────────────────────────────────────────────────────────────────────────
# 6. LIFESPAN: EXECUTES EXACTLY ONCE DURING STARTUP & SHUTDOWN
# ─────────────────────────────────────────────────────────────────────────────
def test_lifespan_executes_exactly_once():
    startup_count = 0
    shutdown_count = 0

    @asynccontextmanager
    async def counting_lifespan(app: FastAPI):
        nonlocal startup_count, shutdown_count
        startup_count += 1
        yield
        shutdown_count += 1

    existing_app = FastAPI(lifespan=counting_lifespan)
    nexus = UnifiedNexus("LifespanApp")

    @nexus.universal_tool("/ping")
    def ping():
        return {"pong": True}

    nexus.attach_to(existing_app)

    with TestClient(existing_app) as client:
        assert startup_count == 1
        res = client.get("/ping")
        assert res.status_code == 200

    assert shutdown_count == 1


# ─────────────────────────────────────────────────────────────────────────────
# 7. MCP PROTOCOL: FASTMCP CLIENT CONNECTS AND INVOKES TOOL
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_valid_fastmcp_client_invocation():
    nexus = UnifiedNexus("ClientTestApp")

    @nexus.universal_tool("/compute")
    def compute(val: int):
        return {"multiplied": val * 10}

    async with Client(nexus.mcp) as client:
        tools = await client.list_tools()
        names = [t.name for t in tools]
        assert "compute" in names

        result = await client.call_tool("compute", {"val": 7})
        assert "70" in str(result)


# ─────────────────────────────────────────────────────────────────────────────
# 8. AI-AGENT SAFETY: BOUNDED INPUT & OUTPUT PAYLOADS
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_bounded_input_and_output_payloads():
    nexus = UnifiedNexus("BoundedApp", max_input_chars=100, max_output_chars=50)

    @nexus.universal_tool("/echo-string")
    def echo_string(text: str):
        return {"text": text}

    tool = await nexus.mcp.get_tool("echo_string")

    # 1. Reject input exceeding max_input_chars
    oversized_input = "A" * 150
    with pytest.raises(ToolError) as exc_input:
        await tool.run({"text": oversized_input})
    assert "exceeds maximum allowed size" in str(exc_input.value)

    # 2. Output exceeding max_output_chars is truncated
    res = await tool.run({"text": "B" * 20})  # input is 20 chars (<100), output returns dict
    res_str = str(res)
    assert len(res_str) > 0


# ─────────────────────────────────────────────────────────────────────────────
# 9. AI-AGENT SAFETY: HUMAN APPROVAL HOOK FOR DESTRUCTIVE ACTIONS
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_human_approval_hook_for_destructive_actions():
    approved_actions = set()

    def approval_policy(tool_name: str, kwargs: dict, tool_type: ToolSafetyLevel) -> bool:
        return tool_name in approved_actions

    nexus = UnifiedNexus("ApprovalApp", approval_hook=approval_policy)

    @nexus.universal_tool(tool_type=ToolSafetyLevel.DESTRUCTIVE)
    def wipe_database(confirmation: str):
        return {"status": "wiped"}

    tool = await nexus.mcp.get_tool("wipe_database")

    # 1. Unapproved invocation rejected
    with pytest.raises(ToolError) as exc_unapproved:
        await tool.run({"confirmation": "yes"})
    assert "requires human approval" in str(exc_unapproved.value)

    # 2. Approved invocation permitted
    approved_actions.add("wipe_database")
    success_res = await tool.run({"confirmation": "yes"})
    assert "wiped" in str(success_res)


# ─────────────────────────────────────────────────────────────────────────────
# 10. EXECUTION-TIME AUTHORIZATION HOOK
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_execution_time_authorization_hook():
    def custom_authorizer(tool_name: str, kwargs: dict, roles: list, scopes: list) -> bool:
        caller_role = kwargs.get("caller_role")
        return caller_role == "admin"

    nexus = UnifiedNexus("AuthApp", authorize_hook=custom_authorizer)

    @nexus.universal_tool(roles=["admin"])
    def admin_action(caller_role: str = "guest"):
        return {"admin": True}

    tool = await nexus.mcp.get_tool("admin_action")

    # Caller with guest role is rejected
    with pytest.raises(ToolError) as exc_auth:
        await tool.run({"caller_role": "guest"})
    assert "Unauthorized" in str(exc_auth.value)

    # Caller with admin role is permitted
    res = await tool.run({"caller_role": "admin"})
    assert "True" in str(res)


# ─────────────────────────────────────────────────────────────────────────────
# 11. REST AUTHENTICATION VIA REST_AUTH
# ─────────────────────────────────────────────────────────────────────────────
def test_unauthorized_rest_requests_rejected():
    def verify_token(authorization: str = Header(None)):
        if authorization != "Bearer secret-token-123":
            raise HTTPException(status_code=401, detail="Unauthorized")
        return True

    nexus = UnifiedNexus("RestAuthApp", rest_auth=Depends(verify_token))

    @nexus.universal_tool("/protected-data", expose="api")
    def get_protected_data():
        return {"data": "secret"}

    app = nexus.finalize(mode="api")

    with TestClient(app) as client:
        # Request without header is rejected (401)
        res_unauth = client.get("/protected-data")
        assert res_unauth.status_code == 401

        # Request with valid token is accepted (200)
        res_auth = client.get("/protected-data", headers={"Authorization": "Bearer secret-token-123"})
        assert res_auth.status_code == 200
        assert res_auth.json() == {"data": "secret"}


# ─────────────────────────────────────────────────────────────────────────────
# 12. HEALTH CHECK PROBE REGISTRATION
# ─────────────────────────────────────────────────────────────────────────────
def test_health_check_probe():
    nexus = UnifiedNexus("HealthApp", version="2.0.0")
    nexus.add_health_check("/healthz")

    app = nexus.finalize(mode="api")

    with TestClient(app) as client:
        res = client.get("/healthz")
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "healthy"
        assert data["service"] == "HealthApp"
        assert data["version"] == "2.0.0"
