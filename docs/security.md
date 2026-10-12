# Enterprise Security & Transport Boundaries

UnifiedNexus enforces strict security boundaries between **FastAPI REST endpoints** and **FastMCP AI-agent tools**. REST and MCP are fundamentally different transport protocols with distinct caller trust models.

---

## 1. Core Security Principle: Dual Transport Isolation

> **Critical Invariant**: REST authentication does **not** automatically protect MCP tools, and MCP authentication does not protect REST endpoints. They must be configured explicitly at their respective boundaries.

```
       Web / Mobile Clients                      AI Agents (Claude / Cursor / LangGraph)
                 │                                                  │
                 ▼ (HTTP REST)                                      ▼ (JSON-RPC / SSE)
         ┌───────────────┐                                  ┌───────────────┐
         │ FastAPI Auth  │                                  │ FastMCP Auth  │
         │ (HTTPBearer / │                                  │ (Token / Auth │
         │  JWT Depends) │                                  │   Provider)   │
         └───────┬───────┘                                  └───────┬───────┘
                 │                                                  │
                 ▼                                                  ▼
     ┌───────────────────────┐                          ┌───────────────────────┐
     │ FastAPI Route Handler │                          │  MCP Security Guard   │
     └───────────┬───────────┘                          └───────────┬───────────┘
                 │                                                  │
                 │          ┌───────────────────────────┐           │
                 └─────────►│  Execution Engine Core   │◄──────────┘
                            │ (Plugins, Bounds, Sem)    │
                            └───────────────────────────┘
```

---

## 2. Fail-Closed Parameter Validation

When functions are exposed to AI agents via MCP (`expose="both"` or `expose="mcp"`), UnifiedNexus strictly validates function signatures at startup:

### Rejection of FastAPI `Depends()` in MCP
In typical FastAPI backends, database sessions and user identities are injected via `Depends(get_db)` or `Depends(get_current_user)`.
- If an agent framework exposes these callables directly to an LLM, the LLM schema would invite the AI model to supply database connections or user identities.
- Attempting to manually invoke `param.default.dependency()` inside an MCP handler without an active HTTP `Request` scope leads to credential bypasses or runtime crashes.

UnifiedNexus rejects any tool containing `Depends()` from MCP registration:
```python
from fastapi import Depends
from unified_nexus import UnifiedNexus

nexus = UnifiedNexus("SecureApp")

# ❌ FAILS AT REGISTRATION with SecurityConfigurationError:
@nexus.universal_tool(path="/user")
def get_user(db = Depends(get_db)):
    return {"status": "ok"}

# ✅ SECURE: Use api_only for REST-specific dependency injection:
@nexus.api_only(path="/user")
def get_user_rest(db = Depends(get_db)):
    return {"status": "ok"}
```

### Rejection of Credential Parameters from LLM Arguments
Parameters named `password`, `token`, `secret`, `api_key`, `access_token`, or `authorization` cannot be registered as MCP tool parameters. This prevents LLMs from requesting caller credentials or hallucinating spoofed auth tokens.

---

## 3. Router Security Isolation

When including existing FastAPI `APIRouter` instances:
```python
router = APIRouter(dependencies=[Depends(verify_internal_api_key)])
```
If this router is included with `nexus.include_router(router, expose="both")`, UnifiedNexus checks router-level dependencies. Routers protected by router-level dependencies cannot be converted to MCP unless explicit MCP authorization policies are provided, preventing accidental unauthenticated agent access.

---

## 4. Dual Authentication Configuration

Configure independent authenticators for both transports:

```python
from fastapi import Depends, Header, HTTPException
from unified_nexus import UnifiedNexus

def verify_jwt(authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid token")
    return {"user_id": 123}

def verify_mcp_token(token: str):
    if token != "secret-mcp-key":
        raise PermissionError("Unauthorized agent")

nexus = UnifiedNexus(
    "ProductionService",
    rest_auth=Depends(verify_jwt),    # Enforced on REST routes
    mcp_auth=verify_mcp_token,        # Enforced on MCP SSE transport
)
```

---

## 5. Caller Anti-Spoofing & Fail-Closed Authorization

To protect against agent prompt-injection and caller argument tampering:
- **No Caller-Controlled Role Arguments**: Tool parameters named `role`, `caller_role`, `user_id`, or `principal` are **never** trusted for authorization.
- **FastMCP Claims Extraction**: When an agent connects using FastMCP authentication, UnifiedNexus automatically extracts token claims (`subject`, `client_id`, `scopes`, and `roles`) into `ctx.principal` and `ctx.permissions`.
- **FastAPI Request State Extraction**: For REST invocations, `request.state.user` and `request.state.permissions` are automatically propagated into `ctx.principal` and `ctx.permissions`.
- **Authorization Hooks**: Applications can supply a custom `authorize_hook(ctx, tool_name, kwargs, roles, scopes)` for fine-grained policy validation.
- **Inherited Router Auth**: When `nexus.include_router(router)` is mounted via `attach_to()` or `finalize()`, `rest_auth` dependencies are propagated automatically to all included REST routes.
- **Fail-Closed Policy**: If any rate limit or authorization hook encounters an error, execution immediately fails closed with `RateLimitExceededError` or `AuthorizationError`.

---

## 6. Secret & Credential Redaction

UnifiedNexus automatically sanitizes error traces and tool outputs to prevent secret leakage:
- PostgreSQL / MySQL connection strings: `postgres://user:***@host/db`
- Bearer tokens: `Bearer ***`
- API keys matching `sk-...`, `ghp-...`, `aws-...` are redacted with `***`
