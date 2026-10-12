"""
05_dependency_injection.py — FastAPI Dependency Injection (Depends) in MCP

Demonstrates:
- Using FastAPI's Depends(...) in tool signatures.
- UnifiedNexus automatically resolving dependencies in-process when invoked via MCP.
- Works with both synchronous and asynchronous dependency functions.
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)) + "/..")

import uvicorn
from fastapi import Depends
from pydantic import BaseModel, Field
from unified_nexus import UnifiedNexus

nexus = UnifiedNexus("DependencyApp")

# 1. Synchronous dependency (e.g. database session or config)
def get_db_session():
    # In production, yield a SQLAlchemy / SQLModel / database session
    return "session_postgresql_main"

# 2. Asynchronous dependency (e.g. auth token verification)
async def get_current_user():
    # In production, decode JWT and fetch user
    return {"user_id": 42, "role": "admin"}

class BalanceRequest(BaseModel):
    account_id: str = Field(..., description="Customer bank account number")

@nexus.universal_tool(path="/account-balance")
async def get_account_balance(
    req: BalanceRequest,
    db: str = Depends(get_db_session),
    user: dict = Depends(get_current_user),
):
    """
    Retrieves the account balance.
    When invoked via HTTP: FastAPI injects db and user.
    When invoked via FastMCP: UnifiedNexus resolves dependencies automatically in-process!
    """
    return {
        "account_id": req.account_id,
        "balance": 15420.50,
        "currency": "USD",
        "verified_by": user["role"],
        "db_connection": db,
    }

app = nexus.finalize()

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
