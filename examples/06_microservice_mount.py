"""
06_microservice_mount.py — Mounting UnifiedNexus to Existing FastAPI Microservices

Demonstrates:
- Attaching UnifiedNexus directly to an existing production FastAPI application.
- Composing existing custom database/Kafka lifespans with FastMCP's task group.
- Using api_prefix for clean microservice versioning (e.g. /api/v1).
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)) + "/..")

from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends
from pydantic import BaseModel
import uvicorn
from unified_nexus import UnifiedNexus

# 1. Existing Production Microservice Application with custom lifespan
db_state = {"connected": False}

@asynccontextmanager
async def database_lifespan(app: FastAPI):
    print("🔌 [Microservice] Connecting to PostgreSQL pool...")
    db_state["connected"] = True
    yield
    print("🛑 [Microservice] Disconnecting PostgreSQL pool...")
    db_state["connected"] = False

existing_app = FastAPI(
    title="InventoryMicroservice",
    version="3.0.0",
    lifespan=database_lifespan,
)

@existing_app.get("/healthz")
def healthz():
    return {"status": "UP", "db": db_state["connected"]}

# 2. Configure UnifiedNexus
nexus = UnifiedNexus("InventoryNexus")

class StockCheck(BaseModel):
    sku: str

@nexus.universal_tool(path="/check-stock", expose="both")
def check_stock(item: StockCheck):
    """Checks warehouse stock for an SKU."""
    return {"sku": item.sku, "available": 128, "in_warehouse": True}

@nexus.api_only(path="/admin-flush")
def flush_cache():
    """Admin maintenance endpoint. REST only."""
    return {"flushed": True}

@nexus.mcp_only
def forecast_demand(sku: str):
    """Predictive inventory model tool for AI assistants."""
    return {"sku": sku, "estimated_runout_days": 14}

# 3. Mount directly to existing application
nexus.attach_to(
    existing_app,
    api_prefix="/api/v1",  # REST routes prefixed: /api/v1/check-stock, /api/v1/admin-flush
    mcp_path="/",          # MCP endpoint available at /mcp
    mode="both",           # Expose both interfaces
)

if __name__ == "__main__":
    uvicorn.run(existing_app, host="127.0.0.1", port=8000, log_level="info")
