"""A post-execution failure must not reopen an idempotent operation for retries."""

import pytest
from fastapi.testclient import TestClient
from fastmcp.exceptions import ToolError

from unified_nexus import NexusPlugin, RedisIdempotencyBackend, ToolSafetyLevel, UnifiedNexus


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["rest", "mcp"])
@pytest.mark.parametrize("failure", ["before_save", "after_save", "during_action"])
async def test_uncertain_action_is_not_reexecuted_by_another_worker(transport, failure):
    fakeredis = pytest.importorskip("fakeredis.aioredis")

    class FailOnceRedis(fakeredis.FakeRedis):
        failed = False

        async def eval(self, script, *args, **kwargs):
            if script == RedisIdempotencyBackend.LUA_COMPLETE and not self.failed:
                self.failed = True
                if failure == "after_save":
                    # The server stored the result, but the response was lost.
                    await super().eval(script, *args, **kwargs)
                raise ConnectionError("Result storage unavailable")
            return await super().eval(script, *args, **kwargs)

    class Identity(NexusPlugin):
        async def before_execute(self, ctx, args):
            ctx.principal = "operator"

    client = FailOnceRedis()
    effects = []

    def make_worker():
        backend = RedisIdempotencyBackend(redis_client=client)
        nexus = UnifiedNexus("StorageFailure", idempotency_backend=backend)
        nexus.add_plugin(Identity())

        @nexus.universal_tool(
            path="/charge", methods=["POST"], tool_type=ToolSafetyLevel.ACTION,
            idempotent=True, timeout=0.01
        )
        async def charge(amount: int, idempotency_key: str):
            effects.append(amount)
            if failure == "during_action":
                raise RuntimeError("Response lost after charge")
            return {"charged": amount}

        return nexus

    first, second = make_worker(), make_worker()
    params = {"amount": 100, "idempotency_key": "tx1"}
    try:
        if transport == "mcp":
            first_tool = await first.mcp.get_tool("charge")
            second_tool = await second.mcp.get_tool("charge")
            with pytest.raises(ToolError):
                await first_tool.run(params)
        else:
            first_http = TestClient(first.finalize(), raise_server_exceptions=False)
            second_http = TestClient(second.finalize(), raise_server_exceptions=False)
            assert first_http.post("/charge", params=params).status_code == 500

        raw = await client.get("nexus:idempotency:operator:charge:tx1")
        assert raw is not None
        expected_status = b"COMPLETED" if failure == "after_save" else b"PENDING"
        assert expected_status in raw

        if transport == "mcp":
            if failure == "after_save":
                result = await second_tool.run(params)
                assert "100" in str(result)
            else:
                # FastMCP may enforce its outer timeout before the pipeline's wait ends.
                with pytest.raises(Exception, match="(?i)timed out"):
                    await second_tool.run(params)
        else:
            response = second_http.post("/charge", params=params)
            if failure == "after_save":
                assert response.status_code == 200
                assert response.json() == {"charged": 100}
            else:
                assert response.status_code == 504

        assert effects == [100]
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_read_only_execution_failure_still_allows_retry():
    nexus = UnifiedNexus("ReadRetry")
    attempts = []

    @nexus.universal_tool(idempotent=True)
    async def read_status(idempotency_key: str):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("Temporary read failure")
        return {"status": "ready"}

    tool = await nexus.mcp.get_tool("read_status")
    with pytest.raises(ToolError):
        await tool.run({"idempotency_key": "read1"})
    await tool.run({"idempotency_key": "read1"})
    assert len(attempts) == 2
