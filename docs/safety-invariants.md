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

### E. In-Memory and Distributed Backends
UnifiedNexus ships with an async/thread-safe in-memory `IdempotencyCache` and defines a standard `BaseIdempotencyBackend` protocol:
```python
from unified_nexus import UnifiedNexus, IdempotencyCache, BaseIdempotencyBackend

# Default built-in in-memory cache (with TTL & LRU eviction)
cache = IdempotencyCache(ttl_seconds=3600.0, maxsize=1000)
nexus = UnifiedNexus("ProductionApp", idempotency_backend=cache)
```
For multi-worker deployments (e.g. multiple Uvicorn workers behind a reverse proxy), plug in a custom `BaseIdempotencyBackend` (e.g., Redis).

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

> [!TIP]
> **Async Recommendation for Side-Effecting Operations**:
> When a timeout fires on an `async def` tool, Python cancels the coroutine immediately. However, synchronous functions running inside worker threads cannot be forcefully killed by the OS.
> For operations with external side-effects (e.g. payment processing, email delivery, or database mutations), **prefer `async def`** so that timeouts immediately halt execution.

