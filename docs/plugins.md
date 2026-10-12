# Extensible Plugin Architecture

UnifiedNexus v0.2.0 introduces a bidirectional plugin architecture. A single plugin intercepts requests and manages lifecycle events identically across **both REST endpoints and MCP AI-agent tool calls**.

---

## 1. The `NexusPlugin` Protocol

Create custom plugins by subclassing `NexusPlugin` and implementing any combination of the lifecycle methods:

```python
from unified_nexus import NexusPlugin, NexusContext

class MyCustomPlugin(NexusPlugin):
    async def startup(self, nexus):
        """Called once during application startup."""
        pass

    async def before_execute(self, context: NexusContext, kwargs: dict) -> dict:
        """
        Invoked immediately before tool or endpoint execution.
        Return modified kwargs or raise an exception to abort execution.
        """
        return kwargs

    async def after_execute(self, context: NexusContext, result: any) -> any:
        """
        Invoked immediately after successful execution.
        Return modified result or pass through.
        """
        return result

    async def on_error(self, context: NexusContext, error: Exception):
        """
        Invoked if execution raises an exception.
        Used for alerting, metrics, or error logging.
        """
        pass

    async def shutdown(self, nexus):
        """Called once during application shutdown."""
        pass
```

Register plugins onto your UnifiedNexus instance using `add_plugin`:
```python
nexus = UnifiedNexus("MyService")
nexus.add_plugin(MyCustomPlugin())
```

---

## 2. The `NexusContext` Object

Every request carries a `NexusContext` instance containing execution metadata:

| Attribute | Type | Description |
| :--- | :--- | :--- |
| `transport` | `Literal['rest', 'mcp']` | The transport protocol invoking the tool (`'rest'` or `'mcp'`). |
| `tool_name` | `str` | Name of the active tool or endpoint function. |
| `request_id` | `str` | Unique UUID4 correlation ID for distributed tracing. |
| `principal` | `Any` | Verified identity of the caller (user ID, tenant ID, or agent identity). |
| `permissions` | `Set[str]` | Scopes, claims, or permissions associated with the principal. |
| `metadata` | `Dict[str, Any]` | Extensible dictionary for plugin state sharing across hooks. |
| `start_time` | `float` | High-resolution timestamp (`time.perf_counter()`) at invocation start. |
| `duration_ms` | `Optional[float]` | Total execution duration in milliseconds (populated in `after_execute`). |

---

## 3. Real-World Plugin Examples

### Example A: Comprehensive Audit Logging Plugin
```python
import logging
from unified_nexus import NexusPlugin, NexusContext

logger = logging.getLogger("audit")

class AuditLogPlugin(NexusPlugin):
    async def before_execute(self, context: NexusContext, kwargs: dict):
        logger.info(
            f"AUDIT START: req_id={context.request_id} "
            f"transport={context.transport} "
            f"tool={context.tool_name} "
            f"principal={context.principal}"
        )

    async def after_execute(self, context: NexusContext, result: any):
        logger.info(
            f"AUDIT SUCCESS: req_id={context.request_id} "
            f"tool={context.tool_name} "
            f"duration={context.duration_ms:.2f}ms"
        )

    async def on_error(self, context: NexusContext, error: Exception):
        logger.error(
            f"AUDIT FAILURE: req_id={context.request_id} "
            f"tool={context.tool_name} "
            f"error={error}"
        )
```

### Example B: Token-Bucket Rate Limiting Plugin
```python
import time
from collections import defaultdict
from unified_nexus import NexusPlugin, NexusContext, RateLimitExceededError

class RateLimitPlugin(NexusPlugin):
    def __init__(self, max_calls_per_minute: int = 60):
        self.limit = max_calls_per_minute
        self.call_history = defaultdict(list)

    async def before_execute(self, context: NexusContext, kwargs: dict):
        key = context.principal or "anonymous"
        now = time.time()
        
        # Purge timestamps older than 60 seconds
        self.call_history[key] = [t for t in self.call_history[key] if now - t < 60]
        
        if len(self.call_history[key]) >= self.limit:
            raise RateLimitExceededError(
                f"Rate limit exceeded: {self.limit} calls/min for {key} on {context.transport}"
            )
            
        self.call_history[key].append(now)
```
