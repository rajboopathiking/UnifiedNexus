import inspect
import logging
from typing import Callable, Any, Optional, List
from fastapi import FastAPI
from fastmcp import FastMCP
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(message)s")
logger = logging.getLogger("unified-nexus")

class UnifiedNexus:
    def __init__(self, name: str, version: str = "1.0.0"):
        self.name = name
        self.api = FastAPI(title=name, version=version)
        self.mcp = FastMCP(name)
        self._endpoints = []

    def universal_tool(
        self, 
        path: str, 
        methods: Optional[List[str]] = None, 
        tags: Optional[List[str]] = None
    ):
        def decorator(func: Callable[..., Any]):
            sig = inspect.signature(func)
            has_pydantic = any(
                isinstance(param.annotation, type) and issubclass(param.annotation, BaseModel) 
                for param in sig.parameters.values()
            )
            
            actual_methods = methods or (["POST"] if has_pydantic else ["GET"])
            actual_tags = tags or ["Unified Interface"]

            @self.mcp.tool(name=func.__name__)
            def mcp_wrapper(*args, **kwargs):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    logger.error(f"❌ MCP Execution Error [{func.__name__}]: {str(e)}")
                    return f"Error: {str(e)}"

            self.api.add_api_route(
                path,
                func,
                methods=actual_methods,
                tags=actual_tags,
                operation_id=func.__name__,
                summary=func.__doc__.strip().split('\n')[0] if func.__doc__ else None
            )
            
            self._endpoints.append({"path": path, "name": func.__name__, "methods": actual_methods})
            return func
        return decorator

    def finalize(self):
        self.mcp.mount_fastapi(self.api)
        
        @self.api.on_event("startup")
        async def startup_event():
            print("\n" + "="*60)
            print(f"🚀 {self.name.upper()} FRAMEWORK ACTIVE")
            print("="*60)
            print(f"🌐 Web API Docs:  http://localhost:8000/docs")
            print(f"🤖 MCP Inspector: npx @modelcontextprotocol/inspector http://localhost:8000/sse")
            print("-"*60)
            for ep in self._endpoints:
                print(f"Registered: {ep['methods']} {ep['path']} -> {ep['name']}")
            print("="*60 + "\n")
            
        return self.api