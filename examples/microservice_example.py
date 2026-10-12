"""
Industry-Standard FastAPI Microservice Integration with UnifiedNexus

Demonstrates how to seamlessly mount UnifiedNexus onto an existing FastAPI microservice,
providing fine-grained exposure control (API only, MCP only, or both) and versioned routing.
"""

import os
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)) + "/..")

from contextlib import asynccontextmanager

import uvicorn
from fastapi import APIRouter, Depends, FastAPI
from pydantic import BaseModel, Field

from unified_nexus import UnifiedNexus


# ── 1. Existing Production Microservice Setup ─────────────────────────────
@asynccontextmanager
async def microservice_lifespan(app: FastAPI):
    print("🔌 [Microservice] Initializing database pool & telemetry...")
    yield
    print("🛑 [Microservice] Closing database connections...")

# Create the standard microservice FastAPI app
app = FastAPI(
    title="CustomerOrderService",
    version="2.4.0",
    description="Production microservice handling customer orders and AI assistance.",
    lifespan=microservice_lifespan,
)

# Standard microservice health probe (K8s liveness/readiness)
@app.get("/healthz", tags=["Ops"])
def health_check():
    return {"status": "UP", "service": "CustomerOrderService"}


# ── 2. UnifiedNexus Configuration & Exposure Control ──────────────────────
nexus = UnifiedNexus("CustomerOrderNexus")

# Database dependency (FastAPI Depends automatically supported in MCP!)
def get_db():
    return "session_postgres_main"

class OrderRequest(BaseModel):
    customer_id: str = Field(..., description="Unique customer identifier")
    item_id: str = Field(..., description="Catalog item SKU")
    quantity: int = Field(1, ge=1, description="Number of items to order")

# [OPTION 1: BOTH] Exposed to Web/Mobile REST clients AND AI Agents via MCP
@nexus.universal_tool(path="/orders", expose="both", tags=["Orders"])
def create_order(req: OrderRequest):
    """Creates a new customer order. Accessible via REST and by AI agents."""
    return {
        "order_id": "ORD-9821",
        "customer": req.customer_id,
        "item": req.item_id,
        "quantity": req.quantity,
        "status": "CONFIRMED",
    }

# [OPTION 2: API ONLY] Traditional REST endpoint with FastAPI Depends()
@nexus.api_only(path="/admin/audit-log", tags=["Admin"])
def get_audit_log(db: str = Depends(get_db)):
    """Internal admin log for human operators with DB session. Not exposed to AI agents."""
    return {"logs": ["admin logged in", "config updated"], "db": db}

# [OPTION 3: MCP ONLY] AI Agent tool (not routed as an HTTP endpoint)
@nexus.mcp_only
def customer_churn_risk(customer_id: str):
    """Predictive churn model tool for AI agents during support chat."""
    return {"customer_id": customer_id, "churn_probability": 0.12, "tier": "VIP"}


# ── 3. Expose Existing Microservice APIRouter to MCP ──────────────────────
catalog_router = APIRouter(tags=["Catalog"])

@catalog_router.get("/inventory")
def check_inventory(sku: str):

    """Checks stock availability in warehouse."""
    return {"sku": sku, "in_stock": 42}

# Automatically exposes router endpoints to AI agents via FastMCP!
nexus.include_router(catalog_router, prefix="/catalog", expose="both")


# ── 4. Mount / Attach to Existing Microservice ────────────────────────────
# Mounts MCP to "/", prefixes REST routes with "/api/v1", and composes lifespans
nexus.attach_to(
    app,
    api_prefix="/api/v1",  # REST routes become /api/v1/orders, etc.
    mcp_path="/",          # MCP endpoint available at /mcp
    mode="both",           # App-level exposure mode
)

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
