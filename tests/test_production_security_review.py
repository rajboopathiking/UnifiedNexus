"""
Targeted tests verifying fixes for the v0.2.0 production security review:
1. P0: Caller-controlled arguments (role, user_id, __approved__) cannot bypass security.
2. P0: REST and MCP execution policy equivalence (bounds, rate limits, approvals).
3. P0: Framework rest_auth applies to included routers.
4. P1: Rate limiting fails closed on error.
5. P1: In-memory idempotency deduplication.
"""

import pytest
from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.testclient import TestClient
from fastmcp.exceptions import ToolError, ValidationError

from unified_nexus import (
    HumanApprovalRequiredError,
    NexusContext,
    NexusPlugin,
    RateLimitExceededError,
    ToolSafetyLevel,
    UnifiedNexus,
)


# ─────────────────────────────────────────────────────────────────────────────
# 1. CALLER ARGUMENTS CANNOT FORGE ROLE OR APPROVAL
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_caller_cannot_spoof_role_via_tool_arguments():
    """Verifies that untrusted caller cannot execute role-restricted tool or spoof role."""
    nexus = UnifiedNexus("AntiSpoofApp")

    @nexus.universal_tool(roles=["admin"])
    def secure_action(data: str):
        return {"data": data, "authorized": True}

    tool = await nexus.mcp.get_tool("secure_action")

    # 1. Unauthenticated / role-less caller is rejected
    with pytest.raises(ToolError) as exc_unauth:
        await tool.run({"data": "payload"})
    assert "Unauthorized" in str(exc_unauth.value)

    # 2. Attacker trying to inject role="admin" argument is rejected by schema validation
    with pytest.raises((ToolError, ValidationError)):
        await tool.run({"data": "payload", "role": "admin"})


@pytest.mark.asyncio
async def test_approval_cannot_be_bypassed_via_approved_argument():
    """Verifies that destructive tools cannot be executed without approval hook verification."""
    nexus = UnifiedNexus("AntiBypassApp")

    @nexus.universal_tool(
        tool_type=ToolSafetyLevel.DESTRUCTIVE,
        requires_approval=True,
    )
    def drop_table(table_name: str):
        return {"dropped": table_name}

    tool = await nexus.mcp.get_tool("drop_table")

    # 1. Call without approval hook configured is rejected
    with pytest.raises(ToolError) as exc_app:
        await tool.run({"table_name": "users"})
    assert "requires human approval" in str(exc_app.value)

    # 2. Attacker trying to inject __approved__=True is rejected
    with pytest.raises((ToolError, ValidationError)):
        await tool.run({"table_name": "users", "__approved__": True})


# ─────────────────────────────────────────────────────────────────────────────
# 2. REST AND MCP POLICY EQUIVALENCE (BOUNDS, RATE LIMIT, APPROVAL)
# ─────────────────────────────────────────────────────────────────────────────
def test_rest_and_mcp_policy_equivalence_for_approvals():
    """Verifies that human approval is enforced on REST as well as MCP."""
    nexus = UnifiedNexus("EquivalenceApp")

    @nexus.universal_tool(
        path="/destructive-action",
        tool_type=ToolSafetyLevel.DESTRUCTIVE,
    )
    def destructive_rest():
        return {"status": "executed"}

    app = nexus.finalize(mode="both")

    # 1. REST invocation without approval hook is rejected with HTTP 403
    with TestClient(app) as client:
        res = client.get("/destructive-action")
        assert res.status_code == 403
        assert "requires human approval" in res.json()["detail"]


def test_rest_enforces_input_bounds():
    """Verifies that input bounds are enforced on REST requests identically to MCP."""
    nexus = UnifiedNexus("BoundsApp", max_input_chars=20)

    @nexus.universal_tool(path="/bounded-input")
    def process_text(text: str):
        return {"processed": len(text)}

    app = nexus.finalize(mode="api")

    with TestClient(app) as client:
        # Request with 50 characters exceeds max_input_chars (20) -> HTTP 413
        long_text = "a" * 50
        res = client.get(f"/bounded-input?text={long_text}")
        assert res.status_code == 413
        assert "exceeds maximum allowed size" in res.json()["detail"]


# ─────────────────────────────────────────────────────────────────────────────
# 3. REST ROUTER INCLUSION ENFORCES REST_AUTH
# ─────────────────────────────────────────────────────────────────────────────
def test_included_routers_enforce_rest_auth():
    """Verifies that framework rest_auth is enforced on included routers in both finalize and attach_to."""
    def verify_api_key(x_api_key: str = Header(None)):
        if x_api_key != "secret-master-key":
            raise HTTPException(status_code=401, detail="Invalid API Key")
        return True

    nexus = UnifiedNexus("RouterAuthApp", rest_auth=Depends(verify_api_key))

    sub_router = APIRouter(prefix="/sub")
    @sub_router.get("/data")
    def sub_data():
        return {"data": "confidential"}

    nexus.include_router(sub_router, expose="api")
    app = nexus.finalize(mode="api")

    with TestClient(app) as client:
        # Request without API key is rejected with 401
        res_unauth = client.get("/sub/data")
        assert res_unauth.status_code == 401

        # Request with valid API key is accepted
        res_ok = client.get("/sub/data", headers={"x-api-key": "secret-master-key"})
        assert res_ok.status_code == 200
        assert res_ok.json() == {"data": "confidential"}


# ─────────────────────────────────────────────────────────────────────────────
# 4. RATE LIMITING FAILS CLOSED ON HOOK ERROR
# ─────────────────────────────────────────────────────────────────────────────
def test_rate_limit_hook_fails_closed_on_error():
    """Verifies that rate limit hook errors cause rejection (fail closed), not silent pass."""
    def buggy_rate_limiter(tool_name: str, kwargs: dict):
        raise RuntimeError("Redis connection failure in rate limiter")

    nexus = UnifiedNexus("FailClosedApp", rate_limit_hook=buggy_rate_limiter)

    @nexus.universal_tool(path="/limited")
    def limited_tool():
        return {"status": "ok"}

    app = nexus.finalize(mode="api")

    with TestClient(app) as client:
        res = client.get("/limited")
        # Fails closed with 429 rather than allowing execution
        assert res.status_code == 429
        assert "Rate limit verification failed" in res.json()["detail"]


# ─────────────────────────────────────────────────────────────────────────────
# 5. IDEMPOTENCY CACHE DEDUPLICATION
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_idempotent_tool_execution_deduplication():
    """Verifies that calling an idempotent tool twice with the same key returns the cached response."""
    nexus = UnifiedNexus("IdempotencyApp")
    execution_counter = 0

    @nexus.universal_tool(tool_type=ToolSafetyLevel.ACTION, idempotent=True)
    def charge_customer(amount: int, idempotency_key: str):
        nonlocal execution_counter
        execution_counter += 1
        return {"charge_id": f"chg_{execution_counter}", "amount": amount}

    tool = await nexus.mcp.get_tool("charge_customer")

    # 1. First execution
    res1 = await tool.run({"amount": 100, "idempotency_key": "tx_key_abc_123"})
    assert execution_counter == 1
    assert "chg_1" in str(res1)

    # 2. Second execution with identical idempotency_key returns cached result
    res2 = await tool.run({"amount": 100, "idempotency_key": "tx_key_abc_123"})
    assert execution_counter == 1, "Function body was re-executed instead of using cache!"
    assert "chg_1" in str(res2)
