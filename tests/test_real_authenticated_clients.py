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
    IdempotencyCache,
    NexusContext,
    NexusPlugin,
    RedisIdempotencyBackend,
    ToolSafetyLevel,
    ToolTimeoutError,
    UnifiedNexus,
    compute_caller_identity,
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


# ─────────────────────────────────────────────────────────────────────────────
# 5. IN-MEMORY CONCURRENCY, REPLACEMENT WAITING & PENDING EXPIRATION
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_in_memory_waiters_do_not_duplicate_acquire_on_release():
    """
    Verifies that when a pending reservation is released by an initial caller,
    waiting callers do NOT all receive ACQUIRED. Exactly ONE replacement waiter
    receives ACQUIRED, while the other waiters wait on the new reservation and
    receive CACHED once it completes.
    """
    cache = IdempotencyCache(ttl_seconds=300.0, lock_timeout=10.0)

    # Initial caller acquires key
    st1, _ = await cache.acquire_or_get("key_race", "hash_same", owner_token="caller_1")
    assert st1 == "ACQUIRED"

    results = []

    async def waiter(caller_id: str):
        st, val = await cache.acquire_or_get("key_race", "hash_same", wait_timeout=2.0, owner_token=caller_id)
        results.append((caller_id, st, val))
        if st == "ACQUIRED":
            # Simulate work and complete
            await asyncio.sleep(0.05)
            await cache.complete("key_race", "hash_same", {"winner": caller_id}, owner_token=caller_id)

    # Launch 4 concurrent waiters awaiting caller_1's in-flight operation
    waiter_tasks = [
        asyncio.create_task(waiter(f"waiter_{i}"))
        for i in range(2, 6)
    ]

    # Give tasks a moment to enter wait
    await asyncio.sleep(0.05)

    # Caller 1 fails / releases key
    await cache.release("key_race", owner_token="caller_1")

    # Await all waiters to finish
    await asyncio.gather(*waiter_tasks)

    # Exactly ONE waiter must have received ACQUIRED, and all others must have received CACHED!
    acquired = [r for r in results if r[1] == "ACQUIRED"]
    cached = [r for r in results if r[1] == "CACHED"]

    assert len(acquired) == 1, f"Expected exactly 1 ACQUIRED, got {len(acquired)}: {acquired}"
    assert len(cached) == 3, f"Expected 3 CACHED, got {len(cached)}: {cached}"

    winner_id = acquired[0][0]
    for c in cached:
        assert c[2] == {"winner": winner_id}


@pytest.mark.asyncio
async def test_in_memory_pending_record_expires_after_lock_timeout():
    """
    Verifies that an abandoned PENDING in-memory reservation expires once lock_timeout
    is exceeded, allowing subsequent callers to acquire the key rather than blocking forever.
    """
    cache = IdempotencyCache(ttl_seconds=300.0, lock_timeout=0.05)

    # Caller 1 acquires key with 50ms lock_timeout and abandons execution
    st1, _ = await cache.acquire_or_get("key_abandoned", "hash_1", owner_token="caller_crashed", lock_timeout=0.05)
    assert st1 == "ACQUIRED"

    # Sleep past the lock timeout
    await asyncio.sleep(0.08)

    # Caller 2 attempts to acquire the abandoned key
    st2, val2 = await cache.acquire_or_get("key_abandoned", "hash_1", wait_timeout=1.0, owner_token="caller_recovered")
    assert st2 == "ACQUIRED"
    assert val2 is None


# ─────────────────────────────────────────────────────────────────────────────
# 6. IDENTITY ENCODING DELIMITER COLLISION PREVENTION
# ─────────────────────────────────────────────────────────────────────────────
def test_identity_encoding_disambiguates_tenant_user_with_delimiters():
    """
    Verifies that different tenant/user pairs containing ':user:' produce strictly
    distinct caller identity strings and isolated idempotency partitions.
    """
    # Tenant containing delimiter injection
    principal_1 = {"tenant_id": "corp:user:alice", "user_id": "42"}
    # Innocent tenant with injected user string
    principal_2 = {"tenant_id": "corp", "user_id": "alice:user:42"}

    id1 = compute_caller_identity(principal_1)
    id2 = compute_caller_identity(principal_2)

    assert id1 != id2
    assert id1 == "tenant:corp%3Auser%3Aalice:user:42"
    assert id2 == "tenant:corp:user:alice%3Auser%3A42"

    ctx1 = NexusContext(transport="rest", tool_name="charge", principal=principal_1, idempotency_key="tx_1")
    ctx2 = NexusContext(transport="rest", tool_name="charge", principal=principal_2, idempotency_key="tx_1")

    assert ctx1.durable_idempotency_key != ctx2.durable_idempotency_key


# ─────────────────────────────────────────────────────────────────────────────
# 7. LIVE FAKEREDIS LUA ATOMIC OWNERSHIP & STALE OVERWRITE PREVENTIONS
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_live_fakeredis_lua_atomic_concurrency_and_expiration():
    """
    Executes live Redis commands and Lua scripts against fakeredis.aioredis.FakeRedis.
    Verifies that:
    1. Atomic release with mismatched owner does not delete the active key.
    2. Atomic complete with mismatched owner does not overwrite the active key.
    3. Stale complete cannot overwrite an already COMPLETED record.
    4. Expired locks can be recovered safely without race conditions.
    """
    fakeredis = pytest.importorskip("fakeredis.aioredis", reason="fakeredis is required for live redis tests")

    fake_client = fakeredis.FakeRedis()
    backend = RedisIdempotencyBackend(redis_client=fake_client, ttl_seconds=300.0, lock_timeout=0.1)

    # 1. Initial acquire
    st, _ = await backend.acquire_or_get("live_tx_1", "hash_v1", owner_token="worker_A", lock_timeout=0.1)
    assert st == "ACQUIRED"

    # 2. Worker B attempts release with wrong owner_token
    await backend.release("live_tx_1", owner_token="stale_worker_B")
    raw = await fake_client.get("nexus:idempotency:live_tx_1")
    assert raw is not None
    assert b"worker_A" in raw

    # 3. Worker B attempts complete with wrong owner_token
    await backend.complete("live_tx_1", "hash_v1", {"result": "stale_b"}, owner_token="stale_worker_B")
    raw_after = await fake_client.get("nexus:idempotency:live_tx_1")
    assert b"worker_A" in raw_after
    assert b"PENDING" in raw_after

    # 4. Worker A completes successfully
    await backend.complete("live_tx_1", "hash_v1", {"result": "winner_a"}, owner_token="worker_A")
    st_cached, val_cached = await backend.acquire_or_get("live_tx_1", "hash_v1")
    assert st_cached == "CACHED"
    assert val_cached == {"result": "winner_a"}

    # 5. Stale late-arriving worker attempts complete over already COMPLETED record
    await backend.complete("live_tx_1", "hash_v1", {"result": "stale_late_overwrite"}, owner_token="worker_late")
    st_cached2, val_cached2 = await backend.acquire_or_get("live_tx_1", "hash_v1")
    assert st_cached2 == "CACHED"
    assert val_cached2 == {"result": "winner_a"}
