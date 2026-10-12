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
git clone <git rep>
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

### Exposure Control: API Only, MCP Only, or Both

Control interface exposure at both the **application level** and **individual tool level**.

#### 1. Server-Level Control
Set the exposure mode during initialization or when calling `finalize()`:

```python
# Option A: Both REST API and MCP (default)
nexus = UnifiedNexus("MyApp", expose="both")

# Option B: REST API Only (disables MCP endpoint and lifespan)
nexus = UnifiedNexus("MyApp", expose="api")

# Option C: MCP Only (disables REST API routes and Swagger docs)
nexus = UnifiedNexus("MyApp", expose="mcp")

# Override at finalization if desired:
app = nexus.finalize(expose="api")  # or expose="mcp", expose="both"
```

| Mode | REST Routes (`/docs`) | MCP Endpoint (`/mcp`) |
|------|----------------------|-----------------------|
| `"both"` | ✅ Active | ✅ Active |
| `"api"` | ✅ Active | ❌ Disabled (404) |
| `"mcp"` | ❌ Disabled (404) | ✅ Active |

#### 2. Tool-Level Control
Decide which interfaces expose specific functions:

```python
# Expose ONLY to REST API
@nexus.universal_tool(path="/web-dashboard", expose="api")
def web_only_data():
    return {"status": "ok"}

# Or use the shortcut decorator:
@nexus.api_only("/metrics")
def get_metrics():
    return {"cpu": 12}

# Expose ONLY to MCP AI Agents (path is optional!)
@nexus.universal_tool(expose="mcp")
def query_vector_store(query: str):
    return {"results": [...]}

# Or use the shortcut decorator:
@nexus.mcp_only
def plan_agent_task(step: str):
    return {"step": step}

# Expose to BOTH REST and MCP (default)
@nexus.universal_tool(path="/shared-tool", expose="both")
def shared_tool():
    return {"shared": True}
```

---

### Industry-Standard Microservice Integration (Mounting to Existing FastAPI Apps)

In real-world microservice architectures, you already have an existing `FastAPI` application with database pools, telemetry, auth dependencies (`Depends`), and custom lifespans. UnifiedNexus can be **mounted directly onto your existing app**:

```python
from fastapi import FastAPI, APIRouter, Depends
from unified_nexus import UnifiedNexus

# 1. Your existing microservice application with custom lifespan
app = FastAPI(title="PaymentMicroservice", version="2.0.0")

# 2. Initialize UnifiedNexus
nexus = UnifiedNexus("PaymentNexus")

# Database dependency (FastAPI Depends is automatically resolved in MCP tool calls!)
def get_db():
    return "session_postgres"

# Fine-grained endpoint control:
@nexus.universal_tool(path="/charge", expose="both")
def charge_card(amount: float, db: str = Depends(get_db)):
    """Exposed to both HTTP REST clients and AI Agents."""
    return {"status": "paid", "amount": amount}

@nexus.api_only(path="/internal-health")
def internal_health():
    """REST only — hidden from AI agents."""
    return {"status": "ok"}

@nexus.mcp_only
def fraud_analysis(account_id: str):
    """MCP only — accessible exclusively by AI agents."""
    return {"risk": "low"}

# Expose an existing microservice APIRouter to MCP:
billing_router = APIRouter()
@billing_router.get("/invoices")
def get_invoices():
    return {"invoices": []}

nexus.include_router(billing_router, prefix="/billing", expose="both")

# 3. Mount directly to your existing FastAPI microservice
nexus.attach_to(
    app,
    api_prefix="/api/v1",  # Prefixes REST routes: /api/v1/charge, /api/v1/billing/invoices
    mcp_path="/",          # MCP endpoint available at /mcp
    mode="both",           # 'both', 'api', or 'mcp'
)

# 4. Your existing ASGI server runs app unchanged:
# uvicorn.run(app, host="0.0.0.0", port=8000)
```

#### Key Microservice Features:
- **Lifespan Composition**: Automatically chains your existing database/Kafka lifespans with FastMCP's task group.
- **`Depends()` Resolution**: FastAPI dependencies in your endpoints are resolved automatically when invoked by AI agents via MCP.
- **Versioned API Prefixing**: `api_prefix="/api/v1"` keeps your REST API versioned while FastMCP tools maintain clean operation names.
- **Full Router Interoperability**: `nexus.include_router(..., expose="both")` instantly makes entire existing APIRouters accessible to AI tools.

---

### LangServe & UnifiedNexus Collaboration (The Killer Advantage)

Modern AI backends typically require **two communication layers**:
1. **LangServe**: Serves LangChain LCEL chains via HTTP (`/invoke`, `/stream`, `/batch`, interactive playground) for web and mobile frontends.
2. **UnifiedNexus (FastMCP)**: Serves native tool calling via Model Context Protocol (`/mcp` SSE) for autonomous AI agents (Claude, Cursor, Copilot, ChatGPT).

Mounting LangServe and UnifiedNexus together on a **single FastAPI app** solves the AI backend dilemma:

```python
from fastapi import FastAPI
from langserve import add_routes
from langchain_core.runnables import RunnableLambda
from unified_nexus import UnifiedNexus

app = FastAPI(title="HybridAIPlatform")
nexus = UnifiedNexus("EnterpriseNexus")

# 1. LangChain LCEL Chain (served via LangServe)
def summarize_doc(data: dict):
    return {"summary": data["text"].upper()}

summary_chain = RunnableLambda(summarize_doc)
add_routes(app, summary_chain, path="/summarize")

# 2. UnifiedNexus Universal Tool (accessible via REST and MCP)
@nexus.universal_tool(path="/search", expose="both")
def search_knowledge_base(query: str):
    """Semantic document search tool."""
    return {"query": query, "results": ["Doc #1", "Doc #2"]}

# 3. FastMCP tool that invokes the LangChain chain in-process!
@nexus.mcp_only
async def agent_summarizer(content: str):
    """Summarizes text using the internal LCEL chain."""
    return await summary_chain.ainvoke({"text": content})

# 4. Attach UnifiedNexus to the same FastAPI app
nexus.attach_to(app, api_prefix="/api/v1", mcp_path="/")

# Run with: uvicorn app:app --port 8000
```

#### Why This Is Game-Changing:
- **Single Process, Single Port**: Web frontends stream LCEL chains via LangServe, while AI agents call tools via MCP.
- **In-Process Chain Execution**: MCP tools can invoke LangChain runnables directly without extra HTTP hops.
- **Zero Drift**: Data schemas and business logic stay unified across both human users and AI agents.

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

## 📂 Feature Examples

The [`examples/`](examples/) directory contains complete, runnable demonstrations for each feature:

| Example | Feature Highlight | Command |
|---|---|---|
| [`01_quickstart.py`](examples/01_quickstart.py) | Basic unified tool, auto GET/POST detection, Swagger & MCP | `python examples/01_quickstart.py` |
| [`02_exposure_modes.py`](examples/02_exposure_modes.py) | Exposure control (`both`, `api`, `mcp`), `@api_only`, `@mcp_only` | `python examples/02_exposure_modes.py` |
| [`03_router_integration.py`](examples/03_router_integration.py) | Modular `APIRouter` support & auto-exposing routers to MCP | `python examples/03_router_integration.py` |
| [`04_middleware_and_security.py`](examples/04_middleware_and_security.py) | Shared CORS/timing middleware, LLM error recovery hints | `python examples/04_middleware_and_security.py` |
| [`05_dependency_injection.py`](examples/05_dependency_injection.py) | Automatic FastAPI `Depends()` resolution in MCP calls | `python examples/05_dependency_injection.py` |
| [`06_microservice_mount.py`](examples/06_microservice_mount.py) | Mounting to existing FastAPI apps, composed lifespans, `/api/v1` | `python examples/06_microservice_mount.py` |
| [`07_stdio_agent_server.py`](examples/07_stdio_agent_server.py) | Stdio transport for local Claude Desktop CLI & Cursor | `python examples/07_stdio_agent_server.py` |
| [`08_langserve_collaboration.py`](examples/08_langserve_collaboration.py) | LangServe LCEL chain + FastMCP tools in the same FastAPI app | `python examples/08_langserve_collaboration.py` |
| [`microservice_example.py`](examples/microservice_example.py) | Production customer order microservice with health probes | `python examples/microservice_example.py` |

---

## 🚀 Production Readiness & PyPI Publishing

UnifiedNexus is **100% production-ready**:
- ✅ **Fully Typed (PEP 561)**: Includes `py.typed` marker for static typing (mypy, pyright).
- ✅ **Packaging**: Standard PEP 621 / Hatchling configuration with clean wheel targets.
- ✅ **Automated Test Coverage**: 24 tests covering edge cases, dependency injection, and lifespans.
- ✅ **Error Resilient**: Converts unexpected exceptions into structured hints for LLMs.

### Building & Publishing to PyPI

```bash
# 1. Build the distribution packages (.tar.gz and .whl)
uv build
# or: python -m build

# 2. Check the built packages
twine check dist/*

# 3. Publish to PyPI
twine upload dist/*
# or: uv publish
```

---

## Testing

Run the comprehensive test suite across Python 3.11 and 3.12:

```bash
pytest tests -v
```

## Requirements

- Python 3.11+
- FastAPI >= 0.115.0
- FastMCP >= 2.0.0
- Uvicorn >= 0.30.0
- Pydantic >= 2.0.0


---

## Contributing

Contributions are welcome! Please open an issue to discuss proposed changes before submitting a PR.
