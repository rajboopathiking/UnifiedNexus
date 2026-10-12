"""
04_middleware_and_security.py — HTTP Middleware, MCP Authorization & Secret Redaction

Demonstrates:
- Adding Starlette/FastAPI middleware (CORS, timing headers) to protect HTTP traffic.
- Understanding the boundary: HTTP middleware protects REST endpoints and /mcp HTTP transport, while FastMCP auth providers protect MCP tool execution.
- LLM-friendly structured error handling with recovery hints and automatic secret redaction.
"""

import os
import sys
import time

sys.path.append(os.path.dirname(os.path.abspath(__file__)) + "/..")

import uvicorn
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from unified_nexus import ToolSafetyLevel, UnifiedNexus

nexus = UnifiedNexus("SecureApp", mask_error_details=True)

# 1. Standard CORS Middleware (Applies to all HTTP traffic including REST and /mcp)
nexus.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 2. Custom Timing Middleware
class TimingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        start = time.perf_counter()
        response = await call_next(request)
        duration = time.perf_counter() - start
        response.headers["X-Response-Time-Ms"] = f"{duration * 1000:.2f}"
        return response

nexus.add_middleware(TimingMiddleware)

# 3. Tool with self-correcting error handling and secret redaction
@nexus.universal_tool(path="/divide", tool_type=ToolSafetyLevel.READ_ONLY, idempotent=True)
def safe_divide(numerator: float, denominator: float):
    """
    Divides two numbers.
    If an error occurs (e.g. division by zero), UnifiedNexus returns a structured
    hint helping LLM agents to self-correct automatically without exposing internal exceptions.
    """
    if denominator == 0:
        raise ValueError("Cannot divide by zero. Please pass a non-zero denominator.")
    return {"result": numerator / denominator}

# 4. Tool demonstrating secret redaction
@nexus.universal_tool(path="/query-database", tool_type=ToolSafetyLevel.READ_ONLY)
def query_database(query: str):
    """Demonstrates that even if database connection strings fail, credentials are never returned."""
    # Simulating connection error with sensitive credentials
    raise RuntimeError("Connection failed to postgresql://dbuser:super_secret_pw@10.0.0.1:5432/analytics")


app = nexus.finalize()

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
