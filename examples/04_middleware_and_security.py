"""
04_middleware_and_security.py — Shared Middleware & Error Recovery

Demonstrates:
- Adding Starlette/FastAPI middleware (CORS, timing headers, custom auth).
- Shared middleware protection across both HTTP REST and FastMCP endpoints.
- LLM-friendly structured error handling with recovery hints.
"""

import sys
import os
import time
sys.path.append(os.path.dirname(os.path.abspath(__file__)) + "/..")

import uvicorn
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from fastapi.middleware.cors import CORSMiddleware
from unified_nexus import UnifiedNexus

nexus = UnifiedNexus("SecureApp")

# 1. Standard CORS Middleware
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

# 3. Tool with self-correcting error handling
@nexus.universal_tool(path="/divide")
def safe_divide(numerator: float, denominator: float):
    """
    Divides two numbers.
    If an error occurs (e.g. division by zero), UnifiedNexus returns a structured
    hint helping LLM agents to self-correct automatically.
    """
    if denominator == 0:
        raise ValueError("Cannot divide by zero. Please pass a non-zero denominator.")
    return {"result": numerator / denominator}

app = nexus.finalize()

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
