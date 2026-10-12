"""
05_dependency_injection.py — Secure Dependency Management in UnifiedNexus v0.2.0

Demonstrates:
- Using FastAPI's Depends(...) securely for REST API endpoints (@nexus.api_only).
- Why attempting to expose FastAPI Depends to MCP raises SecurityConfigurationError (preventing credential leaks & unsandboxed callable execution).
- The recommended pattern for securing MCP tools using explicit authorization policies and roles.
"""

import os
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)) + "/..")

import uvicorn
from fastapi import Depends, Header, HTTPException
from pydantic import BaseModel, Field

from unified_nexus import SecurityConfigurationError, ToolSafetyLevel, UnifiedNexus

nexus = UnifiedNexus("SecureDependencyApp")

# 1. FastAPI REST Dependencies (Database session & JWT bearer auth)
def get_db_session():
    # In production, yields a SQLAlchemy / SQLModel / PostgreSQL session
    return "session_postgresql_main"

def get_current_user(authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    return {"user_id": 42, "role": "admin"}


class BalanceRequest(BaseModel):
    account_id: str = Field(..., description="Customer bank account number")


# 2. REST-Only Endpoint: FastAPI handles Depends() natively and securely
@nexus.api_only(path="/account-balance")
async def get_account_balance_api(
    req: BalanceRequest,
    db: str = Depends(get_db_session),
    user: dict = Depends(get_current_user),
):
    """
    Retrieves account balance via HTTP REST.
    FastAPI resolves dependencies (database connection and JWT header) safely.
    """
    return {
        "account_id": req.account_id,
        "balance": 15420.50,
        "currency": "USD",
        "verified_by": user["role"],
        "db_connection": db,
    }


# 3. MCP Tool: AI-Agent Safe pattern
# Notice: MCP tools must NOT accept credentials or Depends() as LLM tool arguments.
# Instead, use explicit tool safety levels, roles, and bounded inputs.
@nexus.mcp_only(
    tool_type=ToolSafetyLevel.READ_ONLY,
    roles=["finance_analyst", "admin"],
    idempotent=True,
)
def get_public_account_status(account_id: str):
    """
    Queries account status for AI agents without leaking database sessions or credentials.
    Access is controlled via MCP execution-time role authorization.
    """
    return {
        "account_id": account_id,
        "status": "active",
        "currency": "USD",
        "tier": "enterprise",
    }


# 4. Security Verification
# UnifiedNexus proactively catches and rejects insecure configurations:
try:
    @nexus.universal_tool(path="/insecure-endpoint", expose="both")
    def insecure_tool(db=Depends(get_db_session)):
        return db
except SecurityConfigurationError as e:
    print(f"🛡️  [Security Check Passed] Insecure Depends() correctly blocked from MCP: {e}")


app = nexus.finalize()

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
