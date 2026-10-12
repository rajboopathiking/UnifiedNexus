# AI-Agent Safety & Operational Invariants

Serving tools to autonomous AI agents (Claude, LangGraph, Cursor, PydanticAI) introduces operational risks absent from standard client-facing web APIs: hallucinated argument storms, prompt injection context floods, infinite retry loops, and unintended destructive side-effects.

UnifiedNexus enforces six core operational safety invariants at the execution layer.

---

## 1. Tool Safety Levels

Every tool can be classified by risk tier using `ToolSafetyLevel`:

```python
from unified_nexus import UnifiedNexus, ToolSafetyLevel

nexus = UnifiedNexus("SafeOps")

# 1. READ_ONLY: Safe for autonomous agent invocation without human oversight
@nexus.universal_tool(path="/search", tool_type=ToolSafetyLevel.READ_ONLY)
def search_docs(query: str):
    return {"results": []}

# 2. ACTION: State-changing operations; requires audit trail and idempotency
@nexus.universal_tool(path="/orders", tool_type=ToolSafetyLevel.ACTION, idempotent=True)
def create_order(item_id: str, idempotency_key: str):
    return {"order_id": 994}

# 3. DESTRUCTIVE: High-risk operations; requires explicit confirmation token
@nexus.universal_tool(path="/account", tool_type=ToolSafetyLevel.DESTRUCTIVE, requires_approval=True)
def delete_account(account_id: str):
    return {"deleted": True}
```

---

## 2. Human Approval Hook for Destructive Actions

When `requires_approval=True` or `tool_type=ToolSafetyLevel.DESTRUCTIVE` is set, an application-defined approval hook validates whether a human supervisor has authorized the action:

```python
from unified_nexus import HumanApprovalRequiredError

def verify_approval(tool_name: str, kwargs: dict) -> bool:
    # Validate approval token or supervisor signature
    return kwargs.get("approval_token") == "SUPERVISOR_CONFIRMED"

nexus = UnifiedNexus("DestructiveService", approval_hook=verify_approval)
```

- If no `approval_hook` is configured on `UnifiedNexus`, destructive operations immediately raise `HumanApprovalRequiredError` to prevent unreviewed execution.
- Magic arguments like `__approved__=True` are explicitly blocked and cannot bypass verification.

---

## 3. Idempotency Guarantees & Deduplication

AI agents frequently encounter transport timeouts or parse errors, leading to automated retries. Tools decorated with `idempotent=True` publish schema annotations signaling to LLM hosts that repeated calls with the same arguments will not produce duplicate side effects.

Furthermore, UnifiedNexus provides enterprise-grade idempotency deduplication with multi-tenant isolation, cryptographic payload validation, and atomic concurrency locking:

### A. Strict Post-Authorization Enforcement
The idempotency cache is **never** queried before rate limiting, execution-time authorization, and human approvals pass. Unauthenticated or unauthorized callers cannot access cached outputs by guessing or reusing an authorized caller's idempotency key.

### B. Multi-Tenant Key Isolation
Cache keys are automatically partitioned by the caller's verified identity:
```
cache_key = f"{caller_identity}:{tool_name}:{idempotency_key}"
```
Two distinct tenants or users submitting the same idempotency key are completely isolated and cannot read or overwrite each other's data.

### C. Cryptographic Payload Binding
The cache binds each key to a SHA-256 hash of the request parameters. Reusing an existing idempotency key with different arguments raises `IdempotencyConflictError` (HTTP 409 on REST, `ToolError` on MCP).

### D. Atomic In-Flight Locking
If multiple concurrent requests arrive with the identical idempotency key and payload, UnifiedNexus atomically reserves the key. The first request executes the side effect while concurrent callers await completion, guaranteeing the tool executes **exactly once**.

### E. In-Memory and Distributed Backends (`RedisIdempotencyBackend`)
UnifiedNexus ships with an async/thread-safe in-memory `IdempotencyCache` and a distributed `RedisIdempotencyBackend` conforming to `BaseIdempotencyBackend`:

```python
from unified_nexus import UnifiedNexus, IdempotencyCache, RedisIdempotencyBackend

# 1. Single-worker / Local development (built-in in-memory cache with TTL & LRU eviction)
cache = IdempotencyCache(ttl_seconds=3600.0, maxsize=1000)
nexus = UnifiedNexus("SingleWorkerApp", idempotency_backend=cache)

# 2. Multi-worker production clusters (e.g. Uvicorn with multiple workers or Kubernetes pods)
import redis.asyncio as aioredis
redis_client = aioredis.from_url("redis://localhost:6379/0")
redis_backend = RedisIdempotencyBackend(redis_client=redis_client, ttl_seconds=3600.0)

nexus_cluster = UnifiedNexus("ProductionClusterApp", idempotency_backend=redis_backend)
```

`RedisIdempotencyBackend` utilizes Redis `SET key value NX EX` for atomic locking across multiple workers, supports non-blocking polling while in-flight operations complete, detects payload conflicts, and automatically purges abandoned locks on execution failure.

### F. Privileged Tool Identity Isolation
For tools declared with `roles`, `scopes`, `requires_approval`, or classified as `ToolSafetyLevel.DESTRUCTIVE`:
- UnifiedNexus strictly **rejects** unauthenticated callers (`ctx.principal is None`) with `AuthorizationError`.
- Privileged operations never fall back to the shared `"anonymous"` cache partition, completely preventing cross-tenant and unauthenticated idempotency key hijacking.

---

## 4. Bounded Input & Output Payloads

To prevent context window overflow and denial-of-service from runaway LLM generations:

- `max_input_chars` (default `100_000`): Rejects incoming payload strings exceeding size thresholds with `PayloadTooLargeError`.
- `max_output_chars` (default `50_000`): Truncates tool output exceeding limits, appending a warning message instructing the agent to narrow its query.

---

## 5. Concurrency Limiting & Timeouts

Prevent agent swarms from exhausting backend resources:

```python
@nexus.universal_tool(
    path="/heavy-compute",
    timeout=5.0,            # Hard execution timeout in seconds
    concurrency_limit=10,   # Max simultaneous executions
)
async def compute(matrix_id: str):
    ...
```

- If execution exceeds `timeout`, the task is canceled and raises `ToolTimeoutError`.
- If active tasks exceed `concurrency_limit`, incoming calls raise `ConcurrencyLimitError`.

---

## 6. Non-Blocking Sync Execution & Timeout Considerations

In async frameworks, long-running synchronous functions block the entire event loop. UnifiedNexus automatically executes sync tools inside Starlette's `run_in_threadpool`, ensuring async endpoints and MCP heartbeats remain responsive.

> [!WARNING]
> **Operational Risk for Synchronous Destructive Operations**:
> When a timeout occurs on an `async def` tool, Python cancels the coroutine immediately. However, synchronous functions running inside worker threads continue running in the background until completion.
> 
> - UnifiedNexus logs a startup warning whenever a `DESTRUCTIVE` tool is registered synchronously.
> - On timeout, UnifiedNexus emits an explicit operational warning.
> - **Best Practice**: Always define state-modifying tools (`ACTION` and `DESTRUCTIVE`) using `async def`, or pair them with distributed idempotency locks (`RedisIdempotencyBackend`) to prevent duplicate execution during recovery.

