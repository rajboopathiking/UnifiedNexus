import inspect
import logging
import functools
from contextlib import asynccontextmanager
from typing import Callable, Any, Optional, List
from fastapi import FastAPI
from fastmcp import FastMCP
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(message)s")
logger = logging.getLogger("unified-nexus")

class UnifiedNexus:
    def __init__(self, name: str, version: str = "1.0.0"):
        self.name = name
        self.mcp = FastMCP(name)
        self._endpoints = []
        
        # We will initialize the FastAPI app during finalize() 
        # to properly handle the ASGI mounting and lifespan
        self.api = None 
        self._version = version

    def universal_tool(
        self, 
        path: str, 
        methods: Optional[List[str]] = None, 
        tags: Optional[List[str]] = None
    ):
        def decorator(func: Callable[..., Any]):
            # 1. Identify Parameter Types for HTTP Method choice
            sig = inspect.signature(func)
            has_pydantic = any(
                isinstance(param.annotation, type) and issubclass(param.annotation, BaseModel) 
                for param in sig.parameters.values()
            )
            
            actual_methods = methods or (["POST"] if has_pydantic else ["GET"])
            actual_tags = tags or ["Unified Interface"]

            # 2. PRO-GRADE WRAPPER (Preserves signature for FastMCP)
            @functools.wraps(func)
            def unified_wrapper(*args, **kwargs):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    logger.error(f"❌ Execution Error [{func.__name__}]: {str(e)}")
                    return f"Error: {str(e)}"

            # 3. Register to MCP
            self.mcp.tool(name=func.__name__)(unified_wrapper)

            # 4. Save endpoint data to register with FastAPI later
            self._endpoints.append({
                "path": path, 
                "name": func.__name__, 
                "methods": actual_methods,
                "tags": actual_tags,
                "func": func,
                "summary": func.__doc__.strip().split('\n')[0] if func.__doc__ else None
            })
            
            return func
        return decorator

    def finalize(self):
        """Builds the ASGI app by combining FastMCP and FastAPI correctly."""
        
        # 1. Get the raw MCP app (internally serves at /mcp)
        mcp_app = self.mcp.http_app()

        # 2. Combined Lifespan — must include mcp_app.lifespan
        @asynccontextmanager
        async def combined_lifespan(app: FastAPI):
            async with mcp_app.lifespan(app):  # ← THIS initializes the MCP task group
                print("\n" + "="*60)
                print(f"🚀 {self.name.upper()} FRAMEWORK ACTIVE")
                print("="*60)
                print(f"🌐 Web API Docs:  http://localhost:8000/docs")
                print(f"🤖 MCP Server:    http://localhost:8000/mcp")
                print(f"🔍 MCP Inspector: npx -y @modelcontextprotocol/inspector http://localhost:8000/mcp")
                print("-" * 60)
                yield

        # 3. Create the Main FastAPI app
        self.api = FastAPI(title=self.name, lifespan=combined_lifespan)

        # 4. Register API routes FIRST
        for ep in self._endpoints:
            self.api.add_api_route(
                path=ep["path"],
                endpoint=ep["func"],
                methods=ep["methods"],
                operation_id=ep["name"],
                summary=ep["summary"]
            )

        # 5. Mount MCP app at "/" — FastMCP internally registers at /mcp
        self.api.mount("/", mcp_app)

        return self.api