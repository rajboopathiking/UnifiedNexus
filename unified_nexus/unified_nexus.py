import inspect
import logging
import functools
from enum import Enum
from contextlib import asynccontextmanager
from typing import Callable, Any, Optional, List, Union, Set
from fastapi import FastAPI, APIRouter, UploadFile, Request, Response
from fastapi.params import Depends as DependsParam
from fastmcp import FastMCP
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("unified-nexus")


class ExposureMode(str, Enum):
    """Supported exposure modes for UnifiedNexus and individual tools."""
    BOTH = "both"
    API = "api"
    MCP = "mcp"
    API_ONLY = "api"
    MCP_ONLY = "mcp"


def normalize_exposure(val: Any) -> ExposureMode:
    """
    Normalizes a string or ExposureMode enum value into an ExposureMode.
    Supports 'both', 'api', 'api_only', 'api-only', 'mcp', 'mcp_only', 'mcp-only', etc.
    """
    if val is None:
        return ExposureMode.BOTH
    if isinstance(val, ExposureMode):
        return val
    s = str(val).lower().strip().replace("-", "_").replace(" ", "_")
    if s in ("both", "all"):
        return ExposureMode.BOTH
    if s in ("api", "api_only", "http", "rest"):
        return ExposureMode.API
    if s in ("mcp", "mcp_only"):
        return ExposureMode.MCP
    raise ValueError(
        f"Invalid exposure mode: '{val}'. Expected 'both', 'api' ('api_only'), or 'mcp' ('mcp_only')."
    )


class UnifiedNexus:
    def __init__(
        self,
        name: str,
        version: str = "1.0.0",
        app: Optional[FastAPI] = None,
        expose: Optional[Union[str, ExposureMode]] = None,
        mode: Optional[Union[str, ExposureMode]] = None,
    ):
        """
        Initializes a UnifiedNexus instance.

        Args:
            name:    Application name.
            version: Application version.
            app:     Optional existing FastAPI app (for mounting in microservices).
            expose:  Default exposure mode for the app ('both', 'api', or 'mcp').
            mode:    Alias for `expose`.
        """
        self.name = name
        self.version = version
        self._mode = normalize_exposure(expose or mode or ExposureMode.BOTH)
        self.mcp = FastMCP(name)
        self._endpoints = []
        self._routers = []      # holds (router, prefix, tags, expose) dicts
        self._middlewares = []  # holds (middleware_class, kwargs) tuples
        self._registered_mcp_tools: Set[str] = set()
        self._existing_app = app
        self.api = app

    @property
    def mode(self) -> ExposureMode:
        """Current default exposure mode ('both', 'api', or 'mcp')."""
        return self._mode

    @mode.setter
    def mode(self, value: Union[str, ExposureMode]):
        self._mode = normalize_exposure(value)

    @property
    def expose(self) -> ExposureMode:
        """Alias for self.mode."""
        return self._mode

    @expose.setter
    def expose(self, value: Union[str, ExposureMode]):
        self._mode = normalize_exposure(value)

    # ─────────────────────────────────────────────
    # INTERNAL MCP REGISTRATION HELPER
    # ─────────────────────────────────────────────
    def _register_endpoint_to_mcp(
        self,
        func: Callable[..., Any],
        name: Optional[str] = None,
        description: Optional[str] = None,
    ):
        """Registers a function as an MCP tool with async handling and error hints."""
        tool_name = name or func.__name__
        if tool_name in self._registered_mcp_tools:
            return

        sig = inspect.signature(func)
        has_incompatible = any(
            p.annotation in (UploadFile, Request, Response)
            for p in sig.parameters.values()
        )
        if has_incompatible:
            logger.info(
                f"ℹ️  '{tool_name}' has HTTP-specific types (e.g. UploadFile/Request) — skipped MCP registration."
            )
            return

        @functools.wraps(func)
        async def unified_wrapper(*args, **kwargs):
            try:
                # Auto-resolve FastAPI Depends when invoked through MCP
                for p_name, param in sig.parameters.items():
                    val = kwargs.get(p_name)
                    if (
                        (p_name not in kwargs or isinstance(val, DependsParam))
                        and isinstance(param.default, DependsParam)
                        and callable(param.default.dependency)
                    ):
                        dep = param.default.dependency
                        if inspect.iscoroutinefunction(dep):
                            kwargs[p_name] = await dep()
                        else:
                            kwargs[p_name] = dep()

                if inspect.iscoroutinefunction(func):
                    return await func(*args, **kwargs)
                return func(*args, **kwargs)
            except Exception as e:
                logger.error(f"❌ [{tool_name}]: {e}")
                return {
                    "error": str(e),
                    "tool": tool_name,
                    "hint": f"Check the parameters passed to '{tool_name}'. {str(e)}",
                }


        doc = description or (func.__doc__.strip().split("\n")[0] if func.__doc__ else None)
        try:
            self.mcp.tool(name=tool_name, description=doc)(unified_wrapper)
            self._registered_mcp_tools.add(tool_name)
        except Exception as e:
            logger.warning(f"Could not register '{tool_name}' as MCP tool: {e}")

    def _register_router_to_mcp(self, router: APIRouter):
        """Inspects an APIRouter and registers compatible routes as FastMCP tools."""
        from fastapi.routing import APIRoute

        for route in router.routes:
            if isinstance(route, APIRoute):
                self._register_endpoint_to_mcp(
                    func=route.endpoint,
                    name=route.name or route.endpoint.__name__,
                    description=route.summary or route.description or (
                        route.endpoint.__doc__.strip().split("\n")[0]
                        if route.endpoint.__doc__
                        else None
                    ),
                )

    # ─────────────────────────────────────────────
    # DECORATOR
    # ─────────────────────────────────────────────
    def universal_tool(
        self,
        path: Optional[Union[str, Callable[..., Any]]] = None,
        methods: Optional[List[str]] = None,
        tags: Optional[List[str]] = None,
        router: Optional[APIRouter] = None,
        include_in_mcp: Optional[bool] = None,
        expose: Optional[Union[str, ExposureMode]] = None,
        mode: Optional[Union[str, ExposureMode]] = None,
    ):
        """
        Registers a function as a FastAPI route, an MCP tool, or both.

        Args:
            path:           URL path (e.g. "/users"). Defaults to '/<function_name>' for HTTP routes.
            methods:        HTTP methods. Auto-detected if not set.
            tags:           Swagger tags.
            router:         Optional APIRouter to attach this route to.
            include_in_mcp: Legacy flag. If False, equivalent to expose='api'.
            expose:         Exposure option: 'both', 'api' ('api_only'), or 'mcp' ('mcp_only').
            mode:           Alias for `expose`.
        """
        # Support @nexus.universal_tool without parentheses
        if callable(path):
            func = path
            return self.universal_tool(
                path=None,
                methods=methods,
                tags=tags,
                router=router,
                include_in_mcp=include_in_mcp,
                expose=expose,
                mode=mode,
            )(func)

        # Resolve exposure mode for this tool
        if include_in_mcp is False and expose is None and mode is None:
            tool_exposure = ExposureMode.API
        else:
            tool_exposure = normalize_exposure(expose or mode or ExposureMode.BOTH)

        def decorator(func: Callable[..., Any]):
            sig = inspect.signature(func)
            actual_path = path or f"/{func.__name__}"

            # Auto-detect HTTP method
            has_body = any(
                isinstance(p.annotation, type) and (
                    issubclass(p.annotation, BaseModel) or
                    p.annotation is UploadFile or
                    p.annotation is bytes
                )
                for p in sig.parameters.values()
            )
            actual_methods = methods or (["POST"] if has_body else ["GET"])
            actual_tags = tags or ["Unified Interface"]

            # Register with FastMCP if tool_exposure is BOTH or MCP
            if tool_exposure in (ExposureMode.BOTH, ExposureMode.MCP):
                self._register_endpoint_to_mcp(func, name=func.__name__)

            # FastAPI / tool tracking
            self._endpoints.append({
                "path": actual_path,
                "func": func,
                "methods": actual_methods,
                "tags": actual_tags,
                "name": func.__name__,
                "router": router,
                "summary": (
                    func.__doc__.strip().split("\n")[0]
                    if func.__doc__ else None
                ),
                "expose": tool_exposure,
            })

            return func
        return decorator

    def api_only(
        self,
        path: Optional[Union[str, Callable[..., Any]]] = None,
        methods: Optional[List[str]] = None,
        tags: Optional[List[str]] = None,
        router: Optional[APIRouter] = None,
    ):
        """
        Shortcut decorator to register a route ONLY as a FastAPI REST endpoint.
        Can be used as @nexus.api_only('/path') or @nexus.api_only.
        """
        if callable(path):
            func = path
            return self.universal_tool(path=None, methods=methods, tags=tags, router=router, expose=ExposureMode.API)(func)
        return self.universal_tool(
            path=path,
            methods=methods,
            tags=tags,
            router=router,
            expose=ExposureMode.API,
        )

    def mcp_only(
        self,
        func: Optional[Callable[..., Any]] = None,
    ):
        """
        Shortcut decorator to register a tool ONLY in MCP (no FastAPI route).
        Can be used as @nexus.mcp_only or @nexus.mcp_only().
        """
        if callable(func):
            return self.universal_tool(path=None, expose=ExposureMode.MCP)(func)
        return self.universal_tool(
            path=None,
            expose=ExposureMode.MCP,
        )

    # ─────────────────────────────────────────────
    # ROUTER REGISTRATION
    # ─────────────────────────────────────────────
    def include_router(
        self,
        router: APIRouter,
        prefix: str = "",
        tags: Optional[List[str]] = None,
        expose: Optional[Union[str, ExposureMode]] = None,
    ):
        """
        Register an APIRouter with the framework.
        If expose is 'both' or 'mcp', automatically registers the router's endpoints as MCP tools.

        Example:
            nexus.include_router(user_router, prefix="/users", tags=["Users"], expose="both")
        """
        router_exposure = normalize_exposure(expose) if expose else ExposureMode.API
        self._routers.append({
            "router": router,
            "prefix": prefix,
            "tags": tags or [],
            "expose": router_exposure,
        })

        if router_exposure in (ExposureMode.BOTH, ExposureMode.MCP):
            self._register_router_to_mcp(router)

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
    # LIFESPAN INTEGRATION FOR MICROSERVICES
    # ─────────────────────────────────────────────
    def get_lifespan(self, external_lifespan: Optional[Callable[..., Any]] = None):
        """
        Returns a composed lifespan asynccontextmanager that runs FastMCP's task group
        alongside any existing application lifespan (e.g. database connections).

        Usage:
            app = FastAPI(lifespan=nexus.get_lifespan(my_db_lifespan))
        """
        mcp_app = self.mcp.http_app()

        @asynccontextmanager
        async def composed(app: FastAPI):
            async with mcp_app.lifespan(app):
                if external_lifespan:
                    async with external_lifespan(app):
                        yield
                else:
                    yield

        return composed

    combine_lifespan = get_lifespan

    # ─────────────────────────────────────────────
    # MOUNT / ATTACH TO EXISTING FASTAPI APP
    # ─────────────────────────────────────────────
    def attach_to(
        self,
        app: FastAPI,
        api_prefix: str = "",
        mcp_path: str = "/",
        mode: Optional[Union[str, ExposureMode]] = None,
        expose: Optional[Union[str, ExposureMode]] = None,
    ) -> FastAPI:
        """
        Mounts / attaches UnifiedNexus routes, routers, and FastMCP to an existing FastAPI application.
        Ideal for industry-standard microservices with existing FastAPI app instances.

        Args:
            app:        The existing FastAPI application.
            api_prefix: Optional URL prefix for REST routes (e.g. '/api/v1').
            mcp_path:   Mount path for the FastMCP application (default '/').
            mode:       Exposure mode override ('both', 'api', or 'mcp').
            expose:     Alias for `mode`.

        Returns:
            The configured FastAPI application.
        """
        active_mode = normalize_exposure(expose or mode or self.mode)
        self.api = app

        # Scan any routers with expose in BOTH or MCP to ensure routes are registered as MCP tools
        if active_mode in (ExposureMode.BOTH, ExposureMode.MCP):
            for r in self._routers:
                if r["expose"] in (ExposureMode.BOTH, ExposureMode.MCP):
                    self._register_router_to_mcp(r["router"])

        # 1. Register REST routes & routers if mode includes API
        if active_mode in (ExposureMode.BOTH, ExposureMode.API):
            clean_api_prefix = api_prefix.rstrip("/") if api_prefix else ""

            for ep in self._endpoints:
                if ep["expose"] in (ExposureMode.BOTH, ExposureMode.API) and ep.get("path"):
                    clean_path = ep["path"] if ep["path"].startswith("/") else "/" + ep["path"]
                    full_path = f"{clean_api_prefix}{clean_path}" if clean_api_prefix else clean_path
                    target = ep["router"] if ep["router"] else app
                    target.add_api_route(
                        path=full_path,
                        endpoint=ep["func"],
                        methods=ep["methods"],
                        operation_id=ep["name"],
                        tags=ep["tags"],
                        summary=ep["summary"],
                    )

            for r in self._routers:
                clean_r_prefix = r["prefix"] if r["prefix"].startswith("/") else ("/" + r["prefix"] if r["prefix"] else "")
                full_r_prefix = f"{clean_api_prefix}{clean_r_prefix}" if clean_api_prefix else clean_r_prefix
                app.include_router(
                    r["router"],
                    prefix=full_r_prefix,
                    tags=r["tags"],
                )

        # 2. Mount FastMCP and compose lifespan if mode includes MCP
        if active_mode in (ExposureMode.BOTH, ExposureMode.MCP):
            mcp_app = self.mcp.http_app()
            original_lifespan = app.router.lifespan_context

            @asynccontextmanager
            async def composed_lifespan(target_app: FastAPI):
                async with mcp_app.lifespan(target_app):
                    if original_lifespan:
                        async with original_lifespan(target_app):
                            yield
                    else:
                        yield

            app.router.lifespan_context = composed_lifespan
            app.mount(mcp_path, mcp_app)


        # 3. Attach middlewares
        for middleware_class, kwargs in self._middlewares:
            app.add_middleware(middleware_class, **kwargs)

        return app

    # Ergonomic aliases
    mount_to = attach_to
    mount = attach_to

    # ─────────────────────────────────────────────
    # FINALIZE
    # ─────────────────────────────────────────────
    def finalize(
        self,
        app: Optional[FastAPI] = None,
        expose: Optional[Union[str, ExposureMode]] = None,
        mode: Optional[Union[str, ExposureMode]] = None,
        api_prefix: str = "",
        mcp_path: str = "/",
    ) -> FastAPI:
        """
        Builds the unified ASGI app (FastAPI + FastMCP) based on the exposure mode.
        If an existing app was provided, attaches to it.

        Args:
            app:        Optional existing FastAPI app instance.
            expose:     Override exposure mode ('both', 'api', or 'mcp').
            mode:       Alias for `expose`.
            api_prefix: Optional prefix for REST routes (when attaching to existing app).
            mcp_path:   Mount path for MCP (when attaching to existing app).

        Modes:
            - 'both': Exposes both REST API endpoints and MCP endpoint (default).
            - 'api':  Exposes ONLY REST API endpoints. MCP is not mounted.
            - 'mcp':  Exposes ONLY MCP endpoint. REST routes & docs are disabled.
        """
        target_app = app or self._existing_app
        if target_app is not None:
            return self.attach_to(
                app=target_app,
                api_prefix=api_prefix,
                mcp_path=mcp_path,
                mode=mode,
                expose=expose,
            )

        active_mode = normalize_exposure(expose or mode or self.mode)

        # Scan router endpoints to FastMCP if applicable
        if active_mode in (ExposureMode.BOTH, ExposureMode.MCP):
            for r in self._routers:
                if r["expose"] in (ExposureMode.BOTH, ExposureMode.MCP):
                    self._register_router_to_mcp(r["router"])

        # 1. MCP app & Lifespan
        mcp_app = self.mcp.http_app() if active_mode in (ExposureMode.BOTH, ExposureMode.MCP) else None

        @asynccontextmanager
        async def combined_lifespan(app_inst: FastAPI):
            banner_mode = (
                "REST + MCP" if active_mode == ExposureMode.BOTH
                else "API ONLY" if active_mode == ExposureMode.API
                else "MCP ONLY"
            )
            print("\n" + "=" * 60)
            print(f"🚀  {self.name.upper()} v{self.version} ACTIVE [{banner_mode}]")
            print("=" * 60)
            if active_mode in (ExposureMode.BOTH, ExposureMode.API):
                print(f"🌐  REST Docs  → http://localhost:8000/docs")
            if active_mode in (ExposureMode.BOTH, ExposureMode.MCP):
                print(f"🤖  MCP       → http://localhost:8000/mcp")
                print(f"🔍  Inspector → npx -y @modelcontextprotocol/inspector http://localhost:8000/mcp")
            print("-" * 60)

            if mcp_app:
                async with mcp_app.lifespan(app_inst):
                    yield
            else:
                yield

        # 2. Main FastAPI app
        fastapi_kwargs = {
            "title": self.name,
            "version": self.version,
            "lifespan": combined_lifespan,
        }
        if active_mode == ExposureMode.MCP:
            fastapi_kwargs.update({
                "openapi_url": None,
                "docs_url": None,
                "redoc_url": None,
            })

        self.api = FastAPI(**fastapi_kwargs)

        # 3. Attach middlewares
        for middleware_class, kwargs in self._middlewares:
            self.api.add_middleware(middleware_class, **kwargs)

        # 4. Register REST routes & routers if mode includes API
        if active_mode in (ExposureMode.BOTH, ExposureMode.API):
            for ep in self._endpoints:
                if ep["expose"] in (ExposureMode.BOTH, ExposureMode.API) and ep.get("path"):
                    target = ep["router"] if ep["router"] else self.api
                    target.add_api_route(
                        path=ep["path"],
                        endpoint=ep["func"],
                        methods=ep["methods"],
                        operation_id=ep["name"],
                        tags=ep["tags"],
                        summary=ep["summary"],
                    )

            for r in self._routers:
                self.api.include_router(
                    r["router"],
                    prefix=r["prefix"],
                    tags=r["tags"],
                )

        # 5. Mount MCP if mode includes MCP
        if active_mode in (ExposureMode.BOTH, ExposureMode.MCP) and mcp_app:
            self.api.mount("/", mcp_app)

        return self.api

    def run(
        self,
        transport: str = "http",
        host: str = "127.0.0.1",
        port: int = 8000,
        **kwargs,
    ):
        """
        Runs the server.
        If transport='stdio', runs the FastMCP stdio server directly.
        If transport='http', runs the unified ASGI app using uvicorn.
        """
        if transport == "stdio":
            self.mcp.run(transport="stdio", **kwargs)
        else:
            import uvicorn
            app = self.api or self.finalize()
            uvicorn.run(app, host=host, port=port, **kwargs)


__all__ = ["UnifiedNexus", "ExposureMode", "normalize_exposure"]
