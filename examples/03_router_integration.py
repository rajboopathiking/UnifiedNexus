"""
03_router_integration.py — APIRouter & Modular Architecture

Demonstrates:
- Using FastAPI APIRouter to structure larger applications into modules.
- Attaching tools to routers using @nexus.universal_tool(router=...).
- Automatically exposing an existing APIRouter's endpoints to FastMCP tools using include_router(expose="both").
"""

import os
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)) + "/..")

import uvicorn
from fastapi import APIRouter
from pydantic import BaseModel, Field

from unified_nexus import UnifiedNexus

nexus = UnifiedNexus("ModularApp")

# ── 1. Create a users router ──────────────────────────────
users_router = APIRouter()

class UserCreate(BaseModel):
    name: str = Field(..., description="User's full name")
    email: str = Field(..., description="User's email address")

# Attach endpoints to the router via decorator
@nexus.universal_tool(path="/", tags=["Users"], router=users_router)
def list_users():
    """List all registered users."""
    return {"users": ["Alice", "Bob"]}

@nexus.universal_tool(path="/", tags=["Users"], router=users_router)
def create_user(user: UserCreate):
    """Register a new user account."""
    return {"created": True, "name": user.name, "email": user.email}

# Register the router with prefix
nexus.include_router(users_router, prefix="/users", tags=["Users"])


# ── 2. Exposing an existing APIRouter to MCP ──────────────
billing_router = APIRouter()

@billing_router.get("/invoices")
def get_invoices(account_id: str):
    """Retrieve billing invoices for an account."""
    return {"account_id": account_id, "invoices": ["INV-2026-001"]}

# Automatically registers router endpoints into FastMCP tools!
nexus.include_router(billing_router, prefix="/billing", tags=["Billing"], expose="both")

app = nexus.finalize()

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
