"""
Tests for Idempotency Security, Multi-tenant Isolation, and Claims Propagation.
Verifies:
1. Authorization runs strictly before idempotency cache lookup (no cross-user cache access).
2. Human approval runs strictly before idempotency cache lookup.
3. Multi-tenant / cross-user idempotency key isolation (different users with same key do not collide).
4. Payload hash binding: reusing same key with different parameters raises IdempotencyConflictError (409 / ToolError).
5. Atomic concurrent submissions: duplicate concurrent calls execute side-effect exactly once.
6. FastMCP identity and claims propagation into NexusContext.
"""

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from fastmcp.exceptions import ToolError
from pydantic import BaseModel

from unified_nexus import (
    ExposureMode,
    IdempotencyConflictError,
    NexusContext,
    NexusPlugin,
    ToolSafetyLevel,
    UnifiedNexus,
)


def extract_result(res):
    if hasattr(res, "structured_content") and res.structured_content is not None:
        return res.structured_content
    if hasattr(res, "data") and res.data is not None:
        return res.data
    if hasattr(res, "content") and res.content:
        import json
        return json.loads(res.content[0].text)
    return res


@pytest.mark.asyncio
async def test_authorization_enforced_before_idempotency_cache():
    """
    Asserts that an unauthenticated/unauthorized caller cannot receive
    a previously cached response by guessing or reusing an authorized caller's idempotency key.
    """
    executed_count = 0

    async def custom_auth(ctx, tool_name, kwargs, roles, scopes):
        # Only callers with principal 'admin_user' are authorized
        return ctx.principal == "admin_user"

    nexus = UnifiedNexus("AuthBeforeCache", authorize_hook=custom_auth)

    @nexus.universal_tool(path="/secure-op", idempotent=True, roles=["admin"])
    def secure_op(data: str, idempotency_key: str):
        nonlocal executed_count
        executed_count += 1
        return {"result": f"processed:{data}", "count": executed_count}

    nexus.finalize()

    # 1. Simulate authorized admin execution by setting request state
    class MockAdminPlugin(NexusPlugin):
        def __init__(self):
            self.principal = "admin_user"

        async def before_execute(self, ctx, kwargs):
            ctx.principal = self.principal
            if self.principal == "admin_user":
                ctx.permissions.add("admin")

    plugin = MockAdminPlugin()
    nexus.add_plugin(plugin)

    tool = await nexus.mcp.get_tool("secure_op")
    res1 = extract_result(await tool.run({"data": "payload", "idempotency_key": "shared-key-1"}))
    assert res1["count"] == 1

    # 2. Now simulate unauthorized caller (principal = 'unauthorized_user')
    plugin.principal = "unauthorized_user"

    # Unauthorized caller presents the exact same idempotency_key
    with pytest.raises(ToolError) as exc:
        await tool.run({"data": "payload", "idempotency_key": "shared-key-1"})
    assert "Unauthorized" in str(exc.value)


@pytest.mark.asyncio
async def test_human_approval_enforced_before_idempotency_cache():
    """
    Asserts that an unapproved caller cannot retrieve a cached destructive response.
    Human approval must be verified on every invocation.
    """
    nexus = UnifiedNexus("ApprovalBeforeCache")
    approved_flag = True

    def approval_check(tool_name: str, kwargs: dict, tool_type: ToolSafetyLevel) -> bool:
        return approved_flag

    nexus.approval_hook = approval_check

    @nexus.universal_tool(
        tool_type=ToolSafetyLevel.DESTRUCTIVE,
        requires_approval=True,
        idempotent=True,
    )
    def wipe_database(db_name: str, idempotency_key: str):
        return {"status": "wiped", "db": db_name}

    tool = await nexus.mcp.get_tool("wipe_database")

    # 1. First execution with approval = True (caches response)
    res = extract_result(await tool.run({"db_name": "test_db", "idempotency_key": "wipe-key-1"}))
    assert res["status"] == "wiped"

    # 2. Second execution with approval = False
    approved_flag = False
    with pytest.raises(ToolError) as exc:
        await tool.run({"db_name": "test_db", "idempotency_key": "wipe-key-1"})
    assert "requires human approval" in str(exc.value)


@pytest.mark.asyncio
async def test_multi_tenant_idempotency_key_isolation():
    """
    Asserts that two different tenants or users submitting the same idempotency key
    are strictly isolated and cannot see each other's cached responses.
    """
    nexus = UnifiedNexus("MultiTenantIdem")
    current_user = "user_A"

    class TenantInjectorPlugin(NexusPlugin):
        async def before_execute(self, ctx: NexusContext, kwargs: dict):
            ctx.principal = current_user

    nexus.add_plugin(TenantInjectorPlugin())

    @nexus.universal_tool(idempotent=True)
    def transfer_funds(account: str, amount: int, idempotency_key: str):
        return {"recipient": account, "amount": amount, "owner": current_user}

    tool = await nexus.mcp.get_tool("transfer_funds")

    # User A executes transfer
    current_user = "user_A"
    res_a = extract_result(await tool.run({"account": "acc1", "amount": 100, "idempotency_key": "tx-same-key"}))
    assert res_a["owner"] == "user_A"

    # User B executes transfer with identical idempotency_key
    current_user = "user_B"
    res_b = extract_result(await tool.run({"account": "acc1", "amount": 100, "idempotency_key": "tx-same-key"}))
    assert res_b["owner"] == "user_B"
    assert res_b["owner"] != res_a["owner"]


@pytest.mark.asyncio
async def test_idempotency_payload_hash_conflict_detection():
    """
    Asserts that reusing the same idempotency key with different parameters
    raises an IdempotencyConflictError (HTTP 409 / ToolError).
    """
    nexus = UnifiedNexus("PayloadHashConflict")

    class PaymentModel(BaseModel):
        amount: int
        idempotency_key: str

    @nexus.universal_tool(path="/payment", idempotent=True)
    def process_payment(req: PaymentModel):
        return {"charged": req.amount}

    app = nexus.finalize()
    client = TestClient(app)

    # 1. First REST request with amount=100
    r1 = client.post("/payment", json={"amount": 100, "idempotency_key": "idem-pay-1"})
    assert r1.status_code == 200
    assert r1.json() == {"charged": 100}

    # 2. Repeat request with identical amount=100 returns cached response
    r2 = client.post("/payment", json={"amount": 100, "idempotency_key": "idem-pay-1"})
    assert r2.status_code == 200
    assert r2.json() == {"charged": 100}

    # 3. Tampered request with DIFFERENT amount=999 using the same idempotency key
    r3 = client.post("/payment", json={"amount": 999, "idempotency_key": "idem-pay-1"})
    assert r3.status_code == 409
    assert "different request parameters" in r3.json()["detail"]

    # 4. Same conflict check over MCP transport
    tool = await nexus.mcp.get_tool("process_payment")
    with pytest.raises(ToolError) as exc:
        await tool.run({"req": {"amount": 500, "idempotency_key": "idem-pay-1"}})
    assert "different request parameters" in str(exc.value)


@pytest.mark.asyncio
async def test_atomic_concurrent_idempotent_submissions():
    """
    Asserts that multiple concurrent requests with the identical idempotency key
    and payload are executed atomically: exactly ONE side-effect executes,
    and all concurrent callers receive the identical result.
    """
    nexus = UnifiedNexus("AtomicConcurrency")
    side_effect_count = 0

    @nexus.universal_tool(idempotent=True)
    async def create_charge(amount: int, idempotency_key: str):
        nonlocal side_effect_count
        side_effect_count += 1
        # Simulate slight I/O duration to ensure concurrent calls overlap
        await asyncio.sleep(0.1)
        return {"charge_id": f"ch_{idempotency_key}", "runs": side_effect_count}

    tool = await nexus.mcp.get_tool("create_charge")

    # Fire 5 concurrent requests with the identical idempotency_key
    tasks = [
        tool.run({"amount": 50, "idempotency_key": "concurrent-key-99"})
        for _ in range(5)
    ]
    raw_results = await asyncio.gather(*tasks)
    results = [extract_result(r) for r in raw_results]

    # Exactly ONE side effect must have occurred!
    assert side_effect_count == 1

    # All 5 callers must receive the exact same successful result
    for res in results:
        assert res["charge_id"] == "ch_concurrent-key-99"
        assert res["runs"] == 1


@pytest.mark.asyncio
async def test_fastmcp_claims_and_permissions_propagation():
    """
    Asserts that FastMCP authenticated user claims (token, scopes, roles)
    are automatically extracted into NexusContext permissions and principal.
    """
    nexus = UnifiedNexus("McpClaimsTest")

    @nexus.universal_tool(roles=["finance_officer"])
    def approve_budget(department: str):
        return {"approved": department}

    mock_token = MagicMock()
    mock_token.subject = "agent_finance_bot"
    mock_token.client_id = "client_123"
    mock_token.scopes = ["read:budget", "finance_officer"]
    mock_token.claims = {"roles": ["finance_officer"]}

    # When get_access_token returns the mock_token
    with patch("fastmcp.server.dependencies.get_access_token", return_value=mock_token):
        tool = await nexus.mcp.get_tool("approve_budget")
        result = extract_result(await tool.run({"department": "Engineering"}))
        assert result == {"approved": "Engineering"}

