# FastAPI Microservice Integration & Mounting

UnifiedNexus is designed to drop cleanly into existing production FastAPI microservices without rewriting existing architectures, ORMs, or connection pools.

---

## 1. Attaching to an Existing FastAPI App

Use `nexus.attach_to()` to mount your UnifiedNexus tools, routers, and MCP transport directly onto an existing `FastAPI` instance:

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from unified_nexus import UnifiedNexus

@asynccontextmanager
async def database_lifespan(app: FastAPI):
    print("🔌 Opening asyncpg connection pool...")
    yield
    print("🛑 Closing asyncpg connection pool...")

# Existing enterprise microservice
app = FastAPI(title="BillingService", lifespan=database_lifespan)

nexus = UnifiedNexus("BillingNexus")

@nexus.universal_tool(path="/invoices")
def get_invoice(invoice_id: str):
    return {"id": invoice_id, "amount": 100.0}

# Attach to existing app:
nexus.attach_to(
    app,
    api_prefix="/api/v1",  # REST endpoints become /api/v1/invoices
    mcp_path="/",          # MCP available at /mcp without path duplication
    mode="both",           # Serve both REST and MCP
)
```

---

## 2. Lifespan Preservation

FastMCP requires its own internal task group initialized during application lifespan. UnifiedNexus automatically merges the parent application's lifespan with the FastMCP lifespan:
- The parent app's startup logic runs first.
- FastMCP initializes its task group.
- On shutdown, both lifespans are cleanly exited in reverse order.
- The combined lifespan executes **exactly once**.

---

## 3. Router Integration & Exposure Modes

Include existing FastAPI `APIRouter` instances with granular exposure control:

```python
from fastapi import APIRouter
from unified_nexus import UnifiedNexus

admin_router = APIRouter(prefix="/admin")
@admin_router.get("/metrics")
def metrics():
    return {"cpu": 12}

tools_router = APIRouter(prefix="/tools")
@tools_router.get("/calculate")
def calculate(a: int, b: int):
    return {"sum": a + b}

nexus = UnifiedNexus("App")

# Expose admin endpoints to REST ONLY (excluded from AI agents):
nexus.include_router(admin_router, expose="api")

# Expose calculation tools to BOTH REST and MCP:
nexus.include_router(tools_router, expose="both")
```

---

## 4. MCP Mount Path Normalization

UnifiedNexus automatically normalizes Starlette mount paths:
- When mounting onto an application, requests to `http://host:port/mcp` route directly to the FastMCP ASGI application.
- Eliminates the double-prefix bug (`/mcp/mcp`) common in naive FastAPI sub-mounts.
