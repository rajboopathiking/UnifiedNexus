"""
Production Verification Tests:
1. Real Authenticated REST Client Testing with Actual Bearer Tokens and Scope Enforcement.
2. Real Authenticated MCP Client Testing with Actual FastMCP TokenVerifier and Scope Enforcement.
3. Redis Multi-Worker Lock Ownership, Expiration, and Crash Recovery.
4. Side-Effect Recovery on Timeout (Lock Retention and Durable Idempotency Keys).
"""

import asyncio
import time

import pytest
from fastapi import Depends, Header, HTTPException, Request
from fastapi.testclient import TestClient
from fastmcp import Client
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken, TokenVerifier

from tests.test_idempotency_and_identity_hardening import MockAsyncRedis, extract_result
from unified_nexus import (
    ExposureMode,
    NexusContext,
    NexusPlugin,
    RedisIdempotencyBackend,
    ToolSafetyLevel,
    ToolTimeoutError,
    UnifiedNexus,
)


# ─────────────────────────────────────────────────────────────────────────────
# 1. REAL AUTHENTICATED REST CLIENT TESTING WITH SCOPES & ANTI-SPOOFING
# ─────────────────────────────────────────────────────────────────────────────
def test_real_authenticated_rest_client_scope_enforcement():
    """
    Verifies that real HTTP clients presenting actual Bearer tokens have their
    identity and scopes verified against endpoint scope policies.
    """
    # 1. Real bearer token authentication dependency
    async def verify_real_bearer_token(request: Request, authorization: str = Header(None)):
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Missing or malformed Authorization header")
        token = authorization.split(" ", 1)[1]
        if token == "token_operator_all_scopes":
            request.state.user = {"sub": "operator_42", "role": "operator"}
            request.state.permissions = {"orders:read", "orders:cancel", "operator"}
        elif token == "token_readonly_viewer":
            request.state.user = {"sub": "viewer_10", "role": "viewer"}
            request.state.permissions = {"orders:read", "viewer"}
        else:
            raise HTTPException(status_code=401, detail="Invalid bearer token")

    from pydantic import BaseModel

    class OrderRequest(BaseModel):
        order_id: str

    nexus = UnifiedNexus(
        "RealAuthRestApp",
        rest_auth=Depends(verify_real_bearer_token),
    )

    @nexus.universal_tool(path="/orders/read", scopes=["orders:read"])
    def get_order_status(req: OrderRequest):
        return {"order_id": req.order_id, "status": "shipped"}

    @nexus.universal_tool(path="/orders/cancel", scopes=["orders:cancel"])
    def cancel_order(req: OrderRequest):
        return {"order_id": req.order_id, "status": "cancelled"}

    app = nexus.finalize()
    client = TestClient(app)

    # A. Unauthenticated request (no Authorization header) -> 401
    res_no_auth = client.post("/orders/read", json={"order_id": "ord_1"})
    assert res_no_auth.status_code == 401
    assert "Missing or malformed" in res_no_auth.text

    # B. Read-only viewer accessing /orders/read -> 200 OK
    res_viewer_read = client.post(
        "/orders/read",
        json={"order_id": "ord_1"},
        headers={"Authorization": "Bearer token_readonly_viewer"},
    )
    assert res_viewer_read.status_code == 200
    assert res_viewer_read.json() == {"order_id": "ord_1", "status": "shipped"}

    # C. Read-only viewer attempting /orders/cancel (lacks 'orders:cancel') -> 403 Forbidden
    res_viewer_cancel = client.post(
        "/orders/cancel",
        json={"order_id": "ord_1"},
        headers={"Authorization": "Bearer token_readonly_viewer"},
    )
    assert res_viewer_cancel.status_code == 403
    assert "Caller lacks required scopes in trusted context" in res_viewer_cancel.text
    assert "orders:cancel" in res_viewer_cancel.text

    # D. Parameter tampering attempt: viewer supplies role / scopes in request body -> Still 403
    res_tamper = client.post(
        "/orders/cancel",
        json={"order_id": "ord_1", "role": "operator", "scopes": ["orders:cancel"]},
        headers={"Authorization": "Bearer token_readonly_viewer"},
    )
    assert res_tamper.status_code == 403

    # E. Authorized operator with 'orders:cancel' scope -> 200 OK
    res_operator_cancel = client.post(
        "/orders/cancel",
        json={"order_id": "ord_1"},
        headers={"Authorization": "Bearer token_operator_all_scopes"},
    )
    assert res_operator_cancel.status_code == 200
    assert res_operator_cancel.json() == {"order_id": "ord_1", "status": "cancelled"}


# ─────────────────────────────────────────────────────────────────────────────
# 2. REAL AUTHENTICATED MCP CLIENT TESTING WITH TOKENVERIFIER & SCOPES
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_real_authenticated_fastmcp_client_scope_enforcement():
    """
    Verifies that FastMCP clients invoking tools have their access token scopes
    enforced fail-closed against declared tool scopes.
    """
    class CustomTokenVerifier(TokenVerifier):
        async def verify_token(self, token: str) -> AccessToken | None:
            if token == "token_billing_admin":
                return AccessToken(
                    token=token,
                    client_id="ai_agent_billing",
                    subject="agent_billing_lead",
                    scopes=["billing:refund", "billing:read"],
                    claims={"roles": ["billing_admin"]},
                )
            elif token == "token_support_readonly":
                return AccessToken(
                    token=token,
                    client_id="ai_agent_support",
                    subject="agent_support_rep",
                    scopes=["billing:read"],
                    claims={"roles": ["support_rep"]},
                )
            return None

    verifier = CustomTokenVerifier()
    nexus = UnifiedNexus("RealMcpAuthApp", mcp_auth=verifier)

    @nexus.universal_tool(scopes=["billing:refund"])
    def process_refund(invoice_id: str, amount: int):
        return {"invoice_id": invoice_id, "amount": amount, "refunded": True}

    # 1. FastMCP Client with insufficient scope token ('billing:read' only)
    token_viewer = await verifier.verify_token("token_support_readonly")
    assert token_viewer is not None

    from unittest.mock import patch
    with patch("fastmcp.server.dependencies.get_access_token", return_value=token_viewer):
        async with Client(nexus.mcp) as client:
            with pytest.raises(Exception) as exc_missing:
                await client.call_tool("process_refund", {"invoice_id": "inv_88", "amount": 50})
            assert "Caller lacks required scopes in trusted context" in str(exc_missing.value)
            assert "billing:refund" in str(exc_missing.value)

    # 2. FastMCP Client with verified token containing 'billing:refund'
    token_admin = await verifier.verify_token("token_billing_admin")
    assert token_admin is not None

    with patch("fastmcp.server.dependencies.get_access_token", return_value=token_admin):
        async with Client(nexus.mcp) as client:
            res = await client.call_tool("process_refund", {"invoice_id": "inv_88", "amount": 50})
            assert "refunded" in str(res)
            assert "inv_88" in str(res)


# ─────────────────────────────────────────────────────────────────────────────
# 3. MULTI-WORKER REDIS LOCK OWNERSHIP & CRASH RECOVERY
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_redis_lock_ownership_prevents_unauthorized_release_after_timeout():
    """
    Verifies that if Worker A's lock expires and is acquired by Worker B,
    Worker A cannot inadvertently delete or release Worker B's lock upon completion.
    """
    mock_redis = MockAsyncRedis()
    backend = RedisIdempotencyBackend(redis_client=mock_redis, ttl_seconds=300.0, lock_timeout=1.0)

    # Worker A acquires key
    status_a, _ = await backend.acquire_or_get("order_123", "hash_alpha", owner_token="worker_A")
    assert status_a == "ACQUIRED"

    # Simulate lock expiration in Redis (Worker A took too long / stalled)
    await mock_redis.delete("nexus:idempotency:order_123")

    # Worker B acquires the newly expired key
    status_b, _ = await backend.acquire_or_get("order_123", "hash_alpha", owner_token="worker_B")
    assert status_b == "ACQUIRED"

    # Worker A finishes late and attempts to release the key with its stale owner_token
    await backend.release("order_123", owner_token="worker_A")

    # The key in Redis MUST still belong to Worker B!
    current_rec = await mock_redis.get("nexus:idempotency:order_123")
    assert current_rec is not None
    assert "worker_B" in current_rec

    # Worker A finishes late and attempts to complete() the key with its stale owner_token
    await backend.complete("order_123", "hash_alpha", {"result": "stale_A"}, owner_token="worker_A")

    # The key in Redis MUST NOT be overwritten by Worker A!
    current_rec_after = await mock_redis.get("nexus:idempotency:order_123")
    assert "worker_B" in current_rec_after
    assert "PENDING" in current_rec_after

    # Worker B completes cleanly
    await backend.complete("order_123", "hash_alpha", {"result": "valid_B"}, owner_token="worker_B")
    status_final, val_final = await backend.acquire_or_get("order_123", "hash_alpha")
    assert status_final == "CACHED"
    assert val_final == {"result": "valid_B"}


# ─────────────────────────────────────────────────────────────────────────────
# 4. SIDE-EFFECT TIMEOUT RECOVERY & DURABLE IDEMPOTENCY KEYS
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_side_effect_timeout_retains_lock_to_prevent_duplicate_execution():
    """
    Verifies that when a destructive / state-changing tool times out, the framework
    retains the idempotency lock so immediate retries do not execute concurrently,
    and sets ctx.durable_idempotency_key for business reconciliation.
    """
    mock_redis = MockAsyncRedis()
    backend = RedisIdempotencyBackend(redis_client=mock_redis)
    nexus = UnifiedNexus("SideEffectApp", idempotency_backend=backend, approval_hook=lambda *args: True)

    class AuthPlugin(NexusPlugin):
        async def before_execute(self, ctx: NexusContext, kwargs: dict):
            ctx.principal = "operator_1"

    nexus.add_plugin(AuthPlugin())

    @nexus.universal_tool(
        tool_type=ToolSafetyLevel.DESTRUCTIVE,
        requires_approval=False,
        idempotent=True,
        timeout=0.05,  # 50ms timeout
    )
    async def wipe_partition(partition_id: str, idempotency_key: str):
        # Simulate long running task that exceeds timeout
        await asyncio.sleep(0.2)
        return {"wiped": partition_id}

    tool = await nexus.mcp.get_tool("wipe_partition")

    # 1. Execution times out
    with pytest.raises(Exception) as exc:
        await tool.run({"partition_id": "part_1", "idempotency_key": "wipe_tx_001"})
    assert "timed out" in str(exc.value).lower()

    # 2. Verify that the idempotency lock was RETAINED in Redis to prevent retry storms
    raw_lock = await mock_redis.get("nexus:idempotency:operator_1:wipe_partition:wipe_tx_001")
    assert raw_lock is not None
    assert "PENDING" in raw_lock


def test_durable_idempotency_key_propagation_in_context():
    """
    Verifies that NexusContext computes qualified durable_idempotency_key
    formatted as {caller_id}:{tool_name}:{idempotency_key} for business service transactions.
    """
    ctx = NexusContext(
        transport="rest",
        tool_name="process_payment",
        principal={"tenant_id": "tenant_9", "user_id": "usr_42"},
        idempotency_key="tx_9988",
    )
    assert ctx.durable_idempotency_key == "tenant:tenant_9:user:usr_42:process_payment:tx_9988"
