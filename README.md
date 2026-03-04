# Unified Nexus

**Unified Nexus** is a high-performance Python framework that bridges the gap between traditional REST APIs and the new world of LLM-native tools (MCP). Build product-grade backends where your web frontend and AI agents share the exact same logic, schemas, and execution paths — defined once.

---

## The Problem

In a traditional stack, you define a FastAPI route, then **manually re-wrap it** as an MCP tool. Over time, these two definitions drift apart — different validation, different schemas, different behavior. This is **Code Drift**, and it silently breaks your AI integrations.

```
❌ Traditional Stack
├── routes/user.py       ← HTTP logic
├── mcp_tools/user.py    ← Copy-pasted MCP logic (already out of sync)
└── models/user.py       ← Shared? Maybe. Hopefully.
```

```
✅ Unified Nexus
└── main.py              ← One function. One decorator. Both interfaces. Always in sync.
```

---

## Installation

```bash
pip install unified-nexus
```

---

## Quick Start

```python
from UnifiedNexus.unified_nexus import UnifiedNexus
from pydantic import BaseModel, Field

nexus = UnifiedNexus("MyNexusAPI")

class UserRequest(BaseModel):
    user_id: int = Field(..., description="The unique ID of the user")

@nexus.universal_tool(path="/user-info")
def get_user(req: UserRequest):
    """
    Fetches user status and clearance level.
    This docstring is used by both Swagger UI and AI Agents automatically.
    """
    return {"id": req.user_id, "status": "Active", "tier": "Gold"}

app = nexus.finalize()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
```

Run it:

```bash
python main.py
```

| Interface | URL |
|-----------|-----|
| REST API Docs (Swagger) | http://localhost:8000/docs |
| MCP SSE Endpoint | http://localhost:8000/mcp |
| MCP Inspector | `npx -y @modelcontextprotocol/inspector http://localhost:8000/mcp` |

---

## Core Features

### Single Source of Truth
Define your function once. Unified Nexus simultaneously registers it as a FastAPI route and an MCP tool. Validation, schemas, and business logic are identical for HTTP clients and AI agents — no drift, no duplication.

### Auto-Method Detection
Unified Nexus intelligently selects `GET` or `POST` based on your function signature. Functions with a Pydantic model input default to `POST`; pure query-based functions use `GET`. No configuration needed.

### AI-Native Tool Descriptions
Your Python docstrings become rich, structured tool descriptions for MCP hosts like Claude, Cursor, and Copilot. Descriptive `Field(description=...)` annotations on your Pydantic models are passed directly to the LLM — helping it understand *how* to call your tools correctly.

### Shared Middleware
Apply authentication, rate limiting, or logging once at the `UnifiedNexus` level. The same middleware protects both your HTTP API and your MCP interface — no configuration duplication.

### LLM-Optimized Errors
When your tool raises a Python exception, Unified Nexus converts it into a structured natural-language hint that helps LLMs self-correct their tool calls — rather than returning a raw stack trace the model cannot act on.

### Lifespan-Safe MCP Integration
The `finalize()` method correctly composes FastAPI and FastMCP lifespans, ensuring the MCP `StreamableHTTPSessionManager` task group is always initialized before requests arrive. No runtime errors, no manual wiring.

---

## Architecture

```
Your Code (@universal_tool)
        │
        ▼
  UnifiedNexus.finalize()
     ┌──────┴──────┐
     │             │
  FastAPI        FastMCP
  /docs           /mcp
  REST           SSE/MCP
     │             │
     └──────┬──────┘
            │
      Single ASGI App
      (Uvicorn / any ASGI server)
```

---

## Advanced Usage

### Custom Path and Methods

```python
@nexus.universal_tool(path="/search", methods=["GET"])
def search_items(query: str, limit: int = 10):
    """Search the item catalog. Returns a ranked list of results."""
    return {"results": [...], "total": 42}
```

### Shared Auth Middleware

```python
from unified_nexus.middleware import BearerAuthMiddleware

nexus = UnifiedNexus("SecureAPI")
nexus.add_middleware(BearerAuthMiddleware, token="your-secret-token")
```

### Async Support

```python
@nexus.universal_tool(path="/fetch-data")
async def fetch_external(resource_id: str):
    """Fetches data from an external source asynchronously."""
    async with httpx.AsyncClient() as client:
        response = await client.get(f"https://api.example.com/{resource_id}")
    return response.json()
```

---

## Connecting AI Agents

### Claude Desktop (`claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "my-nexus-api": {
      "url": "http://localhost:8000/mcp",
      "transport": "streamable-http"
    }
  }
}
```

### Cursor / VS Code

Add `http://localhost:8000/mcp` as an MCP server in your editor's AI settings.

### Verify with MCP Inspector

```bash
npx -y @modelcontextprotocol/inspector http://localhost:8000/mcp
```

---

**Here's a simple example of how to use APIRouter with UnifiedNexus framework:**
```python
from fastapi import APIRouter
from pydantic import BaseModel
from unified_nexus import UnifiedNexus

nexus = UnifiedNexus("My API")

# ── 1. Create a router ──────────────────────────────────────
user_router = APIRouter()

class UserCreate(BaseModel):
    name: str
    email: str

# ── 2. Attach endpoints to the router using @nexus.universal_tool ──
@nexus.universal_tool(path="/", tags=["Users"], router=user_router)
def get_users():
    """Get all users"""
    return {"users": ["Alice", "Bob"]}

@nexus.universal_tool(path="/{user_id}", tags=["Users"], router=user_router)
def get_user(user_id: int):
    """Get a single user"""
    return {"user_id": user_id, "name": "Alice"}

@nexus.universal_tool(path="/", tags=["Users"], router=user_router)
def create_user(user: UserCreate):
    """Create a new user"""
    return {"message": f"Created {user.name}"}

# ── 3. Register the router with a prefix ────────────────────
nexus.include_router(user_router, prefix="/users", tags=["Users"])

# ── 4. Finalize ─────────────────────────────────────────────
app = nexus.finalize()
```
## Requirements

- Python 3.11+
- FastAPI
- FastMCP
- Uvicorn
- Pydantic v2

---

## Contributing

Contributions are welcome! Please open an issue to discuss proposed changes before submitting a PR.
