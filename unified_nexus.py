import inspect
import logging
import functools
from contextlib import asynccontextmanager
from typing import Callable, Any, Optional, List
from fastapi import FastAPI, APIRouter, UploadFile
from fastmcp import FastMCP
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("unified-nexus")


class UnifiedNexus:
    def __init__(self, name: str, version: str = "1.0.0"):
        self.name = name
        self.version = version
        self.mcp = FastMCP(name)
        self._endpoints = []
        self._routers = []      # holds (APIRouter, prefix, tags) tuples
        self._middlewares = []  # holds (middleware_class, kwargs) tuples
        self.api = None

    # ─────────────────────────────────────────────
    # DECORATOR
    # ─────────────────────────────────────────────
    def universal_tool(
        self,
        path: str,
        methods: Optional[List[str]] = None,
        tags: Optional[List[str]] = None,
        router: Optional[APIRouter] = None,
        include_in_mcp: bool = True,
    ):
        """
        Registers a function as both a FastAPI route and an MCP tool.

        Args:
            path:           URL path (e.g. "/users")
            methods:        HTTP methods. Auto-detected if not set.
            tags:           Swagger tags.
            router:         Optional APIRouter to attach this route to.
            include_in_mcp: Set False to expose only via HTTP (e.g. file uploads).
        """
        def decorator(func: Callable[..., Any]):
            sig = inspect.signature(func)

            # ── Auto-detect HTTP method ──────────────────────────────
            has_body = any(
                isinstance(p.annotation, type) and (
                    issubclass(p.annotation, BaseModel) or
                    p.annotation is UploadFile or
                    p.annotation is bytes
                )
                for p in sig.parameters.values()
            )
            actual_methods = methods or (["POST"] if has_body else ["GET"])
            actual_tags    = tags or ["Unified Interface"]

            # ── Async-safe wrapper ───────────────────────────────────
            @functools.wraps(func)
            async def unified_wrapper(*args, **kwargs):
                try:
                    if inspect.iscoroutinefunction(func):
                        return await func(*args, **kwargs)
                    return func(*args, **kwargs)
                except Exception as e:
                    logger.error(f"❌ [{func.__name__}]: {e}")
                    # LLM-friendly error — helps AI self-correct
                    return {
                        "error": str(e),
                        "tool": func.__name__,
                        "hint": f"Check the parameters passed to '{func.__name__}'. {str(e)}"
                    }

            # ── MCP registration ─────────────────────────────────────
            # UploadFile cannot be serialised for MCP — skip it there.
            has_upload = any(
                p.annotation is UploadFile
                for p in sig.parameters.values()
            )
            if include_in_mcp and not has_upload:
                self.mcp.tool(name=func.__name__)(unified_wrapper)
            elif has_upload:
                logger.info(
                    f"ℹ️  '{func.__name__}' has UploadFile — "
                    "registered as HTTP-only (skipped MCP)."
                )

            # ── FastAPI registration ─────────────────────────────────
            self._endpoints.append({
                "path":    path,
                "func":    func,
                "methods": actual_methods,
                "tags":    actual_tags,
                "name":    func.__name__,
                "router":  router,          # None → attach to main app
                "summary": (
                    func.__doc__.strip().split('\n')[0]
                    if func.__doc__ else None
                ),
            })

            return func
        return decorator

    # ─────────────────────────────────────────────
    # ROUTER REGISTRATION
    # ─────────────────────────────────────────────
    def include_router(
        self,
        router: APIRouter,
        prefix: str = "",
        tags: Optional[List[str]] = None,
    ):
        """
        Register an APIRouter with the framework.
        Call this BEFORE finalize().

        Example:
            nexus.include_router(user_router, prefix="/users", tags=["Users"])
        """
        self._routers.append({"router": router, "prefix": prefix, "tags": tags or []})

    # ─────────────────────────────────────────────
    # MIDDLEWARE
    # ─────────────────────────────────────────────
    def add_middleware(self, middleware_class, **kwargs):
        """
        Add any Starlette/FastAPI middleware.

        Example:
            from fastapi.middleware.cors import CORSMiddleware
            nexus.add_middleware(CORSMiddleware, allow_origins=["*"])
        """
        self._middlewares.append((middleware_class, kwargs))

    # ─────────────────────────────────────────────
    # FINALIZE  ← your proven working implementation
    # ─────────────────────────────────────────────
    def finalize(self) -> FastAPI:
        """Builds the unified ASGI app (FastAPI + FastMCP)."""

        # 1. MCP app — internal route is /mcp by default
        mcp_app = self.mcp.http_app()

        # 2. Composed lifespan — initialises the MCP task group
        @asynccontextmanager
        async def combined_lifespan(app: FastAPI):
            async with mcp_app.lifespan(app):
                print("\n" + "=" * 60)
                print(f"🚀  {self.name.upper()} v{self.version} ACTIVE")
                print("=" * 60)
                print(f"🌐  REST Docs  → http://localhost:8000/docs")
                print(f"🤖  MCP       → http://localhost:8000/mcp")
                print(f"🔍  Inspector → npx -y @modelcontextprotocol/inspector http://localhost:8000/mcp")
                print("-" * 60)
                yield

        # 3. Main FastAPI app
        self.api = FastAPI(
            title=self.name,
            version=self.version,
            lifespan=combined_lifespan,
        )

        # 4. Attach middlewares
        for middleware_class, kwargs in self._middlewares:
            self.api.add_middleware(middleware_class, **kwargs)

        # 5. Register routes — split between main app and routers
        for ep in self._endpoints:
            target = ep["router"] if ep["router"] else self.api
            target.add_api_route(
                path=ep["path"],
                endpoint=ep["func"],
                methods=ep["methods"],
                operation_id=ep["name"],
                tags=ep["tags"],
                summary=ep["summary"],
            )

        # 6. Include routers registered via nexus.include_router()
        for r in self._routers:
            self.api.include_router(
                r["router"],
                prefix=r["prefix"],
                tags=r["tags"],
            )

        # 7. Mount MCP at "/" — proven working (internal path is /mcp)
        self.api.mount("/", mcp_app)

        return self.api
