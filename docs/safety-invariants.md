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

If the hook returns `False`, execution is rejected with `HumanApprovalRequiredError` before the underlying function executes.

---

## 3. Idempotency Guarantees

AI agents frequently encounter transport timeouts or parse errors, leading to automated retries. Tools decorated with `idempotent=True` publish schema annotations signaling to LLM hosts that repeated calls with the same arguments will not produce duplicate side effects.

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

## 6. Non-Blocking Sync Execution

In async frameworks, long-running synchronous functions block the entire event loop. UnifiedNexus automatically executes sync tools inside Starlette's `run_in_threadpool`, ensuring async endpoints and MCP heartbeats remain responsive.
