"""
UnifiedNexus: One decorator. Two interfaces.
Bridges FastAPI and Model Context Protocol (MCP) with enterprise-grade security,
AI-agent safety, robust routing, and microservice lifespan preservation.
"""

import asyncio
import functools
import inspect
import logging
import time
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager
from enum import Enum
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request, Response, UploadFile
from fastapi.params import Depends as DependsParam
from fastapi.routing import APIRoute

try:
    from fastapi.params import Security as SecurityParam
except ImportError:
    SecurityParam = DependsParam
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from unified_nexus.context import NexusContext
from unified_nexus.exceptions import (
    AuthorizationError,
    ConcurrencyLimitError,
    DuplicateRouteError,
    DuplicateToolError,
    HumanApprovalRequiredError,
    IdempotencyConflictError,
    PayloadTooLargeError,
    RateLimitExceededError,
    SecurityConfigurationError,
    ToolTimeoutError,
    UnifiedNexusError,
)
from unified_nexus.plugins import NexusPlugin, PluginManager
from unified_nexus.security import (
    BaseIdempotencyBackend,
    IdempotencyCache,
    ToolSafetyLevel,
    bound_output,
    check_input_bounds,
    compute_caller_identity,
    compute_payload_hash,
    sanitize_message,
    validate_mcp_parameters,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
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
        app: FastAPI | None = None,
        expose: str | ExposureMode | None = None,
        mode: str | ExposureMode | None = None,
        auth: Any | None = None,
        mcp_auth: Any | None = None,
        rest_auth: Any | None = None,
        default_timeout: float = 30.0,
        concurrency_limit: int = 50,
        max_input_chars: int = 100_000,
        max_output_chars: int = 50_000,
        authorize_hook: Callable[..., Any] | None = None,
        approval_hook: Callable[..., Any] | None = None,
        rate_limit_hook: Callable[..., Any] | None = None,
        audit_hook: Callable[..., Any] | None = None,
        mask_error_details: bool = True,
        idempotency_backend: BaseIdempotencyBackend | None = None,
        idempotency_cache: BaseIdempotencyBackend | None = None,
    ):
        """
        Initializes a UnifiedNexus instance.

        Args:
            name:               Application name.
            version:            Application version.
            app:                Optional existing FastAPI app (for mounting in microservices).
            expose:             Default exposure mode for the app ('both', 'api', or 'mcp').
            mode:               Alias for `expose`.
            auth:               Optional shared/MCP authentication provider.
            mcp_auth:           Explicit FastMCP AuthProvider (e.g. TokenVerifier).
            rest_auth:          Explicit FastAPI dependency or security handler for REST routes.
            default_timeout:    Default tool execution timeout in seconds (default 30.0s).
            concurrency_limit:  Max concurrent tool executions (default 50).
            max_input_chars:    Max characters allowed for string inputs (default 100,000).
            max_output_chars:   Max characters before output is truncated (default 50,000).
            authorize_hook:     Optional hook: (tool_name, kwargs, roles, scopes) -> bool.
            approval_hook:      Optional hook for human approval: (tool_name, kwargs, tool_type) -> bool.
            rate_limit_hook:    Optional hook for rate limiting: (tool_name, kwargs) -> bool.
            audit_hook:         Optional hook for audit logging: (record_dict) -> None.
            mask_error_details: Redact raw internal tracebacks/secrets from client responses.
        """
        self.name = name
        self.version = version
        self._mode = normalize_exposure(expose or mode or ExposureMode.BOTH)

        # FastMCP configuration
        effective_mcp_auth = mcp_auth
        if effective_mcp_auth is None and auth is not None:
            try:
                from fastmcp.server.auth import AuthProvider
                if isinstance(auth, AuthProvider):
                    effective_mcp_auth = auth
            except ImportError:
                pass

        self.mcp = FastMCP(
            name,
            auth=effective_mcp_auth,
            mask_error_details=mask_error_details,
        )

        self.rest_auth = rest_auth
        self.default_timeout = default_timeout
        self.max_input_chars = max_input_chars
        self.max_output_chars = max_output_chars
        self.authorize_hook = authorize_hook
        self.approval_hook = approval_hook
        self.rate_limit_hook = rate_limit_hook
        self.audit_hook = audit_hook
        self.mask_error_details = mask_error_details

        self._concurrency_semaphore = asyncio.Semaphore(concurrency_limit)
        self._endpoints: list[dict] = []
        self._routers: list[dict] = []
        self._middlewares: list[tuple[Any, dict]] = []
        self._registered_mcp_tools: set[str] = set()
        self._registered_routes: set[tuple[str, str]] = set()  # (method, full_path)
        self._existing_app = app
        self.api = app
        self._mcp_app: Any | None = None
        self.plugins = PluginManager()
        self._idempotency_cache = idempotency_backend or idempotency_cache or IdempotencyCache()

    def add_plugin(self, plugin: NexusPlugin) -> None:
        """Registers an extensible plugin for UnifiedNexus."""
        self.plugins.add_plugin(plugin)

    @property
    def mode(self) -> ExposureMode:
        """Current default exposure mode ('both', 'api', or 'mcp')."""
        return self._mode

    @mode.setter
    def mode(self, value: str | ExposureMode):
        self._mode = normalize_exposure(value)

    @property
    def expose(self) -> ExposureMode:
        """Alias for self.mode."""
        return self._mode

    @expose.setter
    def expose(self, value: str | ExposureMode):
        self._mode = normalize_exposure(value)

    # ─────────────────────────────────────────────
    # MCP SINGLETON APP & LIFESPAN MANAGEMENT
    # ─────────────────────────────────────────────
    def _get_or_create_mcp_app(self, mcp_path: str = "/") -> Any:
        """
        Returns the cached Starlette ASGI sub-application for FastMCP.
        Normalizes mount paths to eliminate /mcp/mcp duplication and ensures
        a single ASGI instance is reused across mount and lifespan.
        """
        if self._mcp_app is not None:
            return self._mcp_app

        clean = (mcp_path or "/").strip().rstrip("/")
        if clean in ("", "/"):
            internal_path = "/mcp"
        else:
            internal_path = "/"

        self._mcp_app = self.mcp.http_app(path=internal_path)
        return self._mcp_app

    # ─────────────────────────────────────────────
    # UNIFIED EXECUTION PIPELINE (SHARED REST & MCP)
    # ─────────────────────────────────────────────
    async def _execute_unified_pipeline(
        self,
        ctx: NexusContext,
        func: Callable[..., Any],
        args: tuple,
        kwargs: dict,
        tool_type: ToolSafetyLevel,
        requires_approval: bool,
        idempotent: bool,
        effective_timeout: float | None,
        roles: list[str] | None,
        scopes: list[str] | None,
    ) -> Any:
        """
        Single unified execution pipeline for BOTH REST and MCP.
        Enforces identical security boundaries, bounds, timeouts, semaphores,
        rate limiting, human approval, and audit hooks across both transports.
        """
        tool_name = ctx.tool_name
        ctx.start_time = time.perf_counter()

        try:
            # 1. Trigger plugin before_execute hooks
            await self.plugins.trigger_before_execute(ctx, kwargs)

            # 2. Bound input payload size
            check_input_bounds(kwargs, self.max_input_chars)


            # 4. Rate limit check (FAILS CLOSED)
            if self.rate_limit_hook:
                try:
                    if inspect.iscoroutinefunction(self.rate_limit_hook):
                        allowed = await self.rate_limit_hook(tool_name, kwargs)
                    else:
                        allowed = await run_in_threadpool(self.rate_limit_hook, tool_name, kwargs)
                    if not allowed:
                        raise RateLimitExceededError(f"Rate limit exceeded for tool '{tool_name}'.")
                except RateLimitExceededError:
                    raise
                except Exception as e:
                    logger.error(f"[{ctx.request_id}] Rate limit hook failed: {e}")
                    raise RateLimitExceededError(
                        f"Rate limit verification failed for tool '{tool_name}'."
                    )

            # 5. Execution-time authorization check (TRUSTED CONTEXT ONLY)
            if self.authorize_hook:
                try:
                    sig_auth = inspect.signature(self.authorize_hook)
                    if len(sig_auth.parameters) >= 5:
                        auth_call = self.authorize_hook(ctx, tool_name, kwargs, roles, scopes)
                    else:
                        auth_call = self.authorize_hook(tool_name, kwargs, roles, scopes)
                    if inspect.iscoroutinefunction(self.authorize_hook):
                        auth_ok = await auth_call
                    else:
                        auth_ok = auth_call
                    if not auth_ok:
                        raise AuthorizationError(
                            f"Unauthorized: Caller is not authorized to execute tool '{tool_name}'."
                        )
                except AuthorizationError:
                    raise
                except Exception as e:
                    logger.error(f"[{ctx.request_id}] Authorize hook error: {e}")
                    raise AuthorizationError(f"Authorization check failed for tool '{tool_name}'.")

            if roles:
                # Require trusted caller permissions from context (NEVER caller-supplied kwargs)
                if not any(r in ctx.permissions for r in roles):
                    if not self.authorize_hook:
                        raise AuthorizationError(
                            f"Unauthorized: Caller lacks required role in trusted context: {roles}"
                        )

            if scopes:
                # Require declared scopes against verified permissions in ctx.permissions (fail-closed)
                if not set(scopes).issubset(ctx.permissions):
                    if not self.authorize_hook:
                        missing = sorted(list(set(scopes) - ctx.permissions))
                        raise AuthorizationError(
                            f"Unauthorized: Caller lacks required scopes in trusted context: {missing}"
                        )

            # 6. Human approval check for sensitive / destructive operations (TRUSTED PROOF ONLY)
            needs_approval = requires_approval or (tool_type == ToolSafetyLevel.DESTRUCTIVE)
            if needs_approval:
                if not self.approval_hook:
                    raise HumanApprovalRequiredError(
                        f"Action '{tool_name}' is classified as {tool_type.value} and requires human approval, "
                        f"but no approval_hook is configured on the framework."
                    )
                approved = False
                try:
                    sig_app = inspect.signature(self.approval_hook)
                    if len(sig_app.parameters) >= 4:
                        app_call = self.approval_hook(ctx, tool_name, kwargs, tool_type)
                    else:
                        app_call = self.approval_hook(tool_name, kwargs, tool_type)
                    if inspect.iscoroutinefunction(self.approval_hook):
                        approved = await app_call
                    else:
                        approved = app_call
                except Exception as e:
                    logger.error(f"[{ctx.request_id}] Approval hook error: {e}")
                    raise HumanApprovalRequiredError(
                        f"Action '{tool_name}' is classified as {tool_type.value} and requires human approval."
                    )

                if not approved:
                    raise HumanApprovalRequiredError(
                        f"Action '{tool_name}' is classified as {tool_type.value} and requires human approval."
                    )

            # 7. Multi-tenant atomic idempotency deduplication (POST-AUTHORIZATION ONLY)
            idem_key = (
                kwargs.get("idempotency_key")
                or kwargs.get("idempotent_key")
                or ctx.metadata.get("idempotency_key")
            )
            if not idem_key:
                for arg in list(args) + list(kwargs.values()):
                    if hasattr(arg, "idempotency_key"):
                        idem_key = getattr(arg, "idempotency_key")
                        break
                    elif hasattr(arg, "idempotent_key"):
                        idem_key = getattr(arg, "idempotent_key")
                        break
            cache_key = None
            payload_hash = None
            acquired_idempotency = False
            owner_token = str(uuid.uuid4())

            if idempotent and idem_key:
                ctx.idempotency_key = str(idem_key)
                # Reject unauthenticated/missing identities for privileged tools rather than
                # using the shared "anonymous" fallback partition.
                is_privileged = bool(
                    roles
                    or scopes
                    or requires_approval
                    or tool_type == ToolSafetyLevel.DESTRUCTIVE
                )
                if is_privileged and ctx.principal is None:
                    raise AuthorizationError(
                        f"Unauthorized: Authenticated identity required for privileged idempotent tool '{tool_name}'."
                    )

                caller_id = compute_caller_identity(ctx.principal)
                payload_hash = compute_payload_hash(kwargs)
                cache_key = f"{caller_id}:{tool_name}:{idem_key}"

                idem_status, cached_val = await self._idempotency_cache.acquire_or_get(
                    key=cache_key,
                    payload_hash=payload_hash,
                    wait_timeout=min(float(effective_timeout or 30.0), 30.0),
                    owner_token=owner_token,
                    lock_timeout=max(float(effective_timeout or 30.0) * 2.0, 60.0),
                )

                if idem_status == "CONFLICT":
                    raise IdempotencyConflictError(
                        f"Idempotency key '{idem_key}' was previously used with different request parameters."
                    )
                elif idem_status == "TIMEOUT":
                    raise ToolTimeoutError(
                        f"Timed out waiting for concurrent execution of idempotency key '{idem_key}'."
                    )
                elif idem_status == "CACHED":
                    logger.info(
                        f"[{ctx.request_id}] Returning cached idempotent response for key '{idem_key}' "
                        f"to caller '{caller_id}' on {ctx.transport}"
                    )
                    ctx.mark_completed()
                    return await self.plugins.trigger_after_execute(ctx, cached_val)

                # idem_status == "ACQUIRED"
                acquired_idempotency = True

            # 7. Execute tool with concurrency limiting and timeout
            try:
                async with self._concurrency_semaphore:
                    if inspect.iscoroutinefunction(func):
                        exec_coro = func(*args, **kwargs)
                    else:
                        exec_coro = run_in_threadpool(func, *args, **kwargs)

                    try:
                        raw_result = await asyncio.wait_for(exec_coro, timeout=effective_timeout)
                    except (TimeoutError, asyncio.TimeoutError):
                        if not inspect.iscoroutinefunction(func):
                            logger.warning(
                                f"[{ctx.request_id}] Operational Warning: Synchronous tool '{tool_name}' "
                                f"(type={tool_type.value}) timed out after {effective_timeout}s. "
                                f"Worker thread will continue running to completion in thread pool. "
                                f"For side-effecting operations (ACTION/DESTRUCTIVE), use 'async def' "
                                f"to support immediate task cancellation, or employ distributed idempotency."
                            )
                        raise

                ctx.mark_completed()

                # 8. Audit hook on success
                if self.audit_hook:
                    try:
                        audit_record = {
                            "request_id": ctx.request_id,
                            "transport": ctx.transport,
                            "tool": tool_name,
                            "tool_type": tool_type.value,
                            "duration_ms": ctx.duration_ms,
                            "status": "success",
                        }
                        if inspect.iscoroutinefunction(self.audit_hook):
                            await self.audit_hook(audit_record)
                        else:
                            await run_in_threadpool(self.audit_hook, audit_record)
                    except Exception as ae:
                        logger.warning(f"[{ctx.request_id}] Audit hook error: {ae}")

                # 9. Bound output size to avoid LLM context window exhaustion
                bounded_res = bound_output(raw_result, self.max_output_chars)

                # 10. Complete idempotent reservation if key was acquired
                if acquired_idempotency and cache_key and payload_hash:
                    await self._idempotency_cache.complete(
                        cache_key, payload_hash, bounded_res, owner_token=owner_token
                    )

                # 11. Trigger plugin after_execute hooks
                final_res = await self.plugins.trigger_after_execute(ctx, bounded_res)
                return final_res

            except Exception as e:
                # If tool failed with timeout on a side-effecting operation (ACTION or DESTRUCTIVE),
                # do NOT release the reservation immediately. Releasing immediately invites a concurrent
                # retry storm while the background worker thread may still be running.
                is_timeout = isinstance(e, (TimeoutError, asyncio.TimeoutError, ToolTimeoutError))
                is_side_effecting = tool_type in (ToolSafetyLevel.ACTION, ToolSafetyLevel.DESTRUCTIVE)

                if acquired_idempotency and cache_key and not (is_timeout and is_side_effecting):
                    try:
                        await self._idempotency_cache.release(cache_key, owner_token=owner_token)
                    except Exception:
                        pass
                elif acquired_idempotency and cache_key and is_timeout and is_side_effecting:
                    logger.warning(
                        f"[{ctx.request_id}] Side-effecting tool '{tool_name}' timed out. Retaining idempotency "
                        f"lock for key '{cache_key}' to prevent duplicate concurrent execution during recovery."
                    )
                raise

        except asyncio.CancelledError as ce:
            ctx.mark_completed()
            await self.plugins.trigger_on_error(ctx, ce)
            logger.warning(f"[{ctx.request_id}] Tool '{tool_name}' execution was cancelled.")
            raise

        except TimeoutError as te:
            ctx.mark_completed()
            await self.plugins.trigger_on_error(ctx, te)
            duration_ms = (time.perf_counter() - ctx.start_time) * 1000
            logger.error(f"[{ctx.request_id}] Tool '{tool_name}' timed out after {effective_timeout}s.")
            raise ToolTimeoutError(f"Tool '{tool_name}' timed out after {effective_timeout}s.")

        except (
            SecurityConfigurationError,
            AuthorizationError,
            HumanApprovalRequiredError,
            IdempotencyConflictError,
            PayloadTooLargeError,
            RateLimitExceededError,
            ToolTimeoutError,
        ) as domain_err:
            ctx.mark_completed()
            await self.plugins.trigger_on_error(ctx, domain_err)
            duration_ms = (time.perf_counter() - ctx.start_time) * 1000
            logger.warning(
                f"[{ctx.request_id}] Tool '{tool_name}' rejected on {ctx.transport}: {domain_err}"
            )
            raise

        except Exception as e:
            ctx.mark_completed()
            await self.plugins.trigger_on_error(ctx, e)
            duration_ms = (time.perf_counter() - ctx.start_time) * 1000
            sanitized_msg = sanitize_message(str(e))
            logger.error(
                f"[{ctx.request_id}] Error in tool '{tool_name}' on {ctx.transport} "
                f"({duration_ms:.2f}ms): {sanitized_msg}"
            )

            if self.audit_hook:
                try:
                    audit_record = {
                        "request_id": ctx.request_id,
                        "transport": ctx.transport,
                        "tool": tool_name,
                        "tool_type": tool_type.value,
                        "duration_ms": duration_ms,
                        "status": "error",
                        "error": sanitized_msg,
                    }
                    if inspect.iscoroutinefunction(self.audit_hook):
                        await self.audit_hook(audit_record)
                    else:
                        await run_in_threadpool(self.audit_hook, audit_record)
                except Exception:
                    pass

            raise

    # ─────────────────────────────────────────────
    # INTERNAL MCP REGISTRATION HELPER
    # ─────────────────────────────────────────────
    def _register_endpoint_to_mcp(
        self,
        func: Callable[..., Any],
        name: str | None = None,
        description: str | None = None,
        tool_type: ToolSafetyLevel = ToolSafetyLevel.READ_ONLY,
        requires_approval: bool = False,
        idempotent: bool = False,
        timeout: float | None = None,
        roles: list[str] | None = None,
        scopes: list[str] | None = None,
        auth: Any | None = None,
        explicit_mcp: bool = False,
    ):
        """
        Registers a function as an MCP tool with security validation,
        concurrency limits, bounded I/O, secret redaction, and audit logging.
        """
        tool_name = name or func.__name__

        # 1. Detect duplicate MCP tools
        if tool_name in self._registered_mcp_tools:
            raise DuplicateToolError(
                f"Duplicate MCP tool registration: tool '{tool_name}' is already registered."
            )

        sig = inspect.signature(func)

        # 2. Check for HTTP-specific parameters (UploadFile, Request, Response)
        has_incompatible = any(
            p.annotation in (UploadFile, Request, Response)
            for p in sig.parameters.values()
        )
        if has_incompatible:
            if explicit_mcp:
                raise SecurityConfigurationError(
                    f"Cannot register '{tool_name}' to MCP: Parameter types (UploadFile/Request/Response) "
                    f"are HTTP-specific and cannot be serialized over MCP."
                )
            logger.info(
                f"ℹ️  '{tool_name}' has HTTP-specific types (e.g. UploadFile/Request) — safely skipped MCP registration."
            )
            return

        # 3. Reject unsupported FastAPI Depends/Security and credential parameters
        validate_mcp_parameters(tool_name, sig.parameters)

        effective_timeout = timeout or self.default_timeout

        @functools.wraps(func)
        async def mcp_wrapper(*args, **kwargs):
            request_id = str(uuid.uuid4())
            ctx = NexusContext(
                transport="mcp",
                tool_name=tool_name,
                request_id=request_id,
                permissions=set(),
                metadata={"tool_type": tool_type.value},
            )

            # Automatic extraction of FastMCP authenticated identity & claims into NexusContext
            try:
                from fastmcp.server.dependencies import get_access_token, get_http_request
                token = get_access_token()
                if token:
                    ctx.principal = getattr(token, "subject", None) or getattr(token, "client_id", None)
                    if getattr(token, "scopes", None):
                        ctx.permissions.update(token.scopes)
                    claims = getattr(token, "claims", None)
                    if isinstance(claims, dict):
                        roles_claim = claims.get("roles") or claims.get("role") or []
                        if isinstance(roles_claim, str):
                            ctx.permissions.add(roles_claim)
                        elif isinstance(roles_claim, (list, set, tuple)):
                            ctx.permissions.update(roles_claim)
                        perms_claim = claims.get("permissions") or []
                        if isinstance(perms_claim, str):
                            ctx.permissions.add(perms_claim)
                        elif isinstance(perms_claim, (list, set, tuple)):
                            ctx.permissions.update(perms_claim)
                else:
                    try:
                        req = get_http_request()
                        if req and hasattr(req, "scope"):
                            user = req.scope.get("user")
                            if user:
                                ctx.principal = user
                            if "permissions" in req.scope:
                                ctx.permissions.update(req.scope["permissions"])
                    except Exception:
                        pass
            except Exception:
                pass

            try:
                from fastmcp.server.dependencies import get_context
                mcp_ctx = get_context()
                if mcp_ctx:
                    if not ctx.principal and getattr(mcp_ctx, "client_id", None):
                        ctx.principal = mcp_ctx.client_id
                    if getattr(mcp_ctx, "request_id", None):
                        ctx.metadata["mcp_request_id"] = mcp_ctx.request_id
            except Exception:
                pass

            try:
                return await self._execute_unified_pipeline(
                    ctx=ctx,
                    func=func,
                    args=args,
                    kwargs=kwargs,
                    tool_type=tool_type,
                    requires_approval=requires_approval,
                    idempotent=idempotent,
                    effective_timeout=effective_timeout,
                    roles=roles,
                    scopes=scopes,
                )
            except (
                SecurityConfigurationError,
                AuthorizationError,
                HumanApprovalRequiredError,
                IdempotencyConflictError,
                PayloadTooLargeError,
                RateLimitExceededError,
                ToolTimeoutError,
                TimeoutError,
            ) as domain_err:
                raise ToolError(str(domain_err))
            except ToolError:
                raise
            except Exception as e:
                sanitized_msg = sanitize_message(str(e))
                # MUST raise ToolError so FastMCP signals error to client (isError: true)
                raise ToolError(f"Error executing tool '{tool_name}': {sanitized_msg}")

        doc = description or (func.__doc__.strip().split("\n")[0] if func.__doc__ else None)
        mcp_tool_auth = auth

        try:
            self.mcp.tool(
                name=tool_name,
                description=doc,
                timeout=effective_timeout,
                auth=mcp_tool_auth,
            )(mcp_wrapper)
            self._registered_mcp_tools.add(tool_name)
        except Exception as e:
            logger.warning(f"Could not register '{tool_name}' as MCP tool: {e}")
            raise

    def _register_router_to_mcp(self, router: APIRouter, prefix: str = ""):
        """Inspects an APIRouter and registers compatible routes as FastMCP tools."""
        # 1. Reject routers with security dependencies from silent/unauthenticated MCP conversion
        if router.dependencies:
            raise SecurityConfigurationError(
                f"Cannot register router '{prefix}' to MCP: Router has {len(router.dependencies)} "
                f"router-level security dependencies. FastAPI dependencies cannot be evaluated by MCP tools. "
                f"Use expose='api' for secured routers, or configure explicit MCP authorization policies."
            )

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
                    explicit_mcp=False,
                )

    # ─────────────────────────────────────────────
    # DECORATORS
    # ─────────────────────────────────────────────
    def universal_tool(
        self,
        path: str | Callable[..., Any] | None = None,
        methods: list[str] | None = None,
        tags: list[str] | None = None,
        router: APIRouter | None = None,
        include_in_mcp: bool | None = None,
        expose: str | ExposureMode | None = None,
        mode: str | ExposureMode | None = None,
        tool_type: ToolSafetyLevel | str = ToolSafetyLevel.READ_ONLY,
        requires_approval: bool = False,
        idempotent: bool = False,
        timeout: float | None = None,
        roles: list[str] | None = None,
        scopes: list[str] | None = None,
        auth: Any | None = None,
        name: str | None = None,
        description: str | None = None,
    ):
        """
        Registers a function as a FastAPI route, an MCP tool, or both.

        Args:
            path:               URL path (e.g. "/users"). Defaults to '/<function_name>' for HTTP routes.
            methods:            HTTP methods. Auto-detected if not set.
            tags:               Swagger tags.
            router:             Optional APIRouter to attach this route to.
            include_in_mcp:     Legacy flag. If False, equivalent to expose='api'.
            expose:             Exposure option: 'both', 'api' ('api_only'), or 'mcp' ('mcp_only').
            mode:               Alias for `expose`.
            tool_type:          Tool safety level ('read_only', 'action', 'destructive').
            requires_approval:  Require explicit approval for execution.
            idempotent:         Declares operation is safe for retry.
            timeout:            Tool execution timeout in seconds.
            roles:              Required authorization roles.
            scopes:             Required authorization scopes.
            auth:               Explicit FastMCP AuthCheck for tool.
            name:               Custom name for the tool/route (defaults to function name).
            description:        Custom description for Swagger docs and MCP tool schema.
        """
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
                tool_type=tool_type,
                requires_approval=requires_approval,
                idempotent=idempotent,
                timeout=timeout,
                roles=roles,
                scopes=scopes,
                auth=auth,
                name=name,
                description=description,
            )(func)

        # Resolve exposure mode for this tool
        if include_in_mcp is False and expose is None and mode is None:
            tool_exposure = ExposureMode.API
        else:
            tool_exposure = normalize_exposure(expose or mode or ExposureMode.BOTH)

        safety_level = ToolSafetyLevel(tool_type) if isinstance(tool_type, str) else tool_type

        def decorator(func: Callable[..., Any]):
            sig = inspect.signature(func)
            actual_path = path or f"/{func.__name__}"
            tool_name = name or func.__name__

            if safety_level == ToolSafetyLevel.DESTRUCTIVE and not inspect.iscoroutinefunction(func):
                logger.warning(
                    f"Tool '{tool_name}' is classified as DESTRUCTIVE but implemented as a synchronous function. "
                    f"In Python, timed-out threadpool worker threads continue running in the background. "
                    f"It is strongly recommended to define DESTRUCTIVE tools using 'async def' to support "
                    f"prompt task cancellation and prevent orphaned state mutations on timeout."
                )

            tool_doc = description or (
                func.__doc__.strip().split("\n")[0]
                if func.__doc__ else None
            )

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

            # 1. Register with FastMCP if tool_exposure is BOTH or MCP
            if tool_exposure in (ExposureMode.BOTH, ExposureMode.MCP):
                self._register_endpoint_to_mcp(
                    func=func,
                    name=tool_name,
                    description=tool_doc,
                    tool_type=safety_level,
                    requires_approval=requires_approval,
                    idempotent=idempotent,
                    timeout=timeout,
                    roles=roles,
                    scopes=scopes,
                    auth=auth,
                    explicit_mcp=(tool_exposure == ExposureMode.MCP),
                )

            # 2. Check for duplicate REST route configurations if exposed to API
            if tool_exposure in (ExposureMode.BOTH, ExposureMode.API):
                clean_path = actual_path if actual_path.startswith("/") else "/" + actual_path
                for method in actual_methods:
                    route_key = (method.upper(), clean_path)
                    if route_key in self._registered_routes:
                        raise DuplicateRouteError(
                            f"Duplicate REST route registration: [{method.upper()}] '{clean_path}' "
                            f"is already registered."
                        )
                    self._registered_routes.add(route_key)

            # 3. Track endpoint registration
            self._endpoints.append({
                "path": actual_path,
                "func": func,
                "methods": actual_methods,
                "tags": actual_tags,
                "name": tool_name,
                "router": router,
                "summary": (
                    func.__doc__.strip().split("\n")[0]
                    if func.__doc__ else None
                ),
                "expose": tool_exposure,
                "tool_type": safety_level,
                "requires_approval": requires_approval,
                "idempotent": idempotent,
                "timeout": timeout,
                "roles": roles,
                "scopes": scopes,
            })

            return func
        return decorator

    def api_only(
        self,
        path: str | Callable[..., Any] | None = None,
        methods: list[str] | None = None,
        tags: list[str] | None = None,
        router: APIRouter | None = None,
    ):
        """Shortcut decorator to register a route ONLY as a FastAPI REST endpoint."""
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
        func: Callable[..., Any] | None = None,
        tool_type: ToolSafetyLevel | str = ToolSafetyLevel.READ_ONLY,
        requires_approval: bool = False,
        idempotent: bool = False,
        timeout: float | None = None,
        roles: list[str] | None = None,
        scopes: list[str] | None = None,
        auth: Any | None = None,
    ):
        """Shortcut decorator to register a tool ONLY in MCP (no FastAPI route)."""
        if callable(func):
            return self.universal_tool(
                path=None,
                expose=ExposureMode.MCP,
                tool_type=tool_type,
                requires_approval=requires_approval,
                idempotent=idempotent,
                timeout=timeout,
                roles=roles,
                scopes=scopes,
                auth=auth,
            )(func)

        def decorator(f: Callable[..., Any]):
            return self.universal_tool(
                path=None,
                expose=ExposureMode.MCP,
                tool_type=tool_type,
                requires_approval=requires_approval,
                idempotent=idempotent,
                timeout=timeout,
                roles=roles,
                scopes=scopes,
                auth=auth,
            )(f)
        return decorator

    # ─────────────────────────────────────────────
    # ROUTER REGISTRATION
    # ─────────────────────────────────────────────
    def include_router(
        self,
        router: APIRouter,
        prefix: str = "",
        tags: list[str] | None = None,
        expose: str | ExposureMode | None = None,
    ):
        """
        Registers an APIRouter with the framework.
        If expose is 'both' or 'mcp', validates and registers router endpoints as MCP tools.
        Guarantees that MCP-only routers are never accidentally mounted to REST.
        """
        router_exposure = normalize_exposure(expose) if expose else ExposureMode.API
        clean_prefix = prefix.rstrip("/") if prefix else ""

        self._routers.append({
            "router": router,
            "prefix": clean_prefix,
            "tags": tags or [],
            "expose": router_exposure,
        })

        if router_exposure in (ExposureMode.BOTH, ExposureMode.MCP):
            self._register_router_to_mcp(router, prefix=clean_prefix)

    # ─────────────────────────────────────────────
    # MIDDLEWARE & HEALTH CHECK
    # ─────────────────────────────────────────────
    def add_middleware(self, middleware_class, **kwargs):
        """Add any Starlette/FastAPI middleware."""
        self._middlewares.append((middleware_class, kwargs))

    def add_health_check(self, path: str = "/healthz"):
        """Registers a standardized health/readiness probe endpoint for Kubernetes / clouds."""
        @self.universal_tool(path=path, expose=ExposureMode.BOTH, tool_type=ToolSafetyLevel.READ_ONLY)
        def health_probe():
            return {
                "status": "healthy",
                "service": self.name,
                "version": self.version,
                "mode": self.mode.value,
            }
        return health_probe

    def _create_rest_endpoint_wrapper(self, ep: dict) -> Callable[..., Any]:
        """Creates an execution wrapper for REST endpoints that executes the unified pipeline."""
        func = ep["func"]
        tool_name = ep["name"]
        tool_type = ep["tool_type"]
        requires_approval = ep.get("requires_approval", False)
        idempotent = ep.get("idempotent", False)
        timeout = ep.get("timeout") or self.default_timeout
        roles = ep.get("roles")
        scopes = ep.get("scopes")

        sig = inspect.signature(func)
        has_req_param = any(p.annotation is Request for p in sig.parameters.values())

        if not has_req_param:
            new_params = list(sig.parameters.values()) + [
                inspect.Parameter("__nexus_req__", inspect.Parameter.KEYWORD_ONLY, annotation=Request, default=None)
            ]
            wrapper_sig = sig.replace(parameters=new_params)
        else:
            wrapper_sig = sig

        @functools.wraps(func)
        async def rest_wrapper(*args, **kwargs):
            req = kwargs.pop("__nexus_req__", None)
            if req is None:
                for arg in args:
                    if isinstance(arg, Request):
                        req = arg
                        break
            if req is None and "request" in kwargs and isinstance(kwargs["request"], Request):
                req = kwargs["request"]

            request_id = str(uuid.uuid4())
            ctx = NexusContext(
                transport="rest",
                tool_name=tool_name,
                request_id=request_id,
                permissions=set(),
                metadata={"tool_type": tool_type.value},
            )

            # Extract trusted caller identity and scopes/permissions from Request state
            if req is not None and hasattr(req, "state"):
                if hasattr(req.state, "user"):
                    ctx.principal = req.state.user
                elif hasattr(req.state, "principal"):
                    ctx.principal = req.state.principal
                if hasattr(req.state, "permissions"):
                    if isinstance(req.state.permissions, (list, tuple, set)):
                        ctx.permissions.update(req.state.permissions)
                    elif isinstance(req.state.permissions, str):
                        ctx.permissions.add(req.state.permissions)
                if hasattr(req.state, "scopes"):
                    if isinstance(req.state.scopes, (list, tuple, set)):
                        ctx.permissions.update(req.state.scopes)
                    elif isinstance(req.state.scopes, str):
                        ctx.permissions.update(req.state.scopes.split())

            # Auto-extract roles if principal is a dict with roles/scopes
            if isinstance(ctx.principal, dict):
                principal_roles = ctx.principal.get("roles") or ctx.principal.get("role") or []
                if isinstance(principal_roles, str):
                    ctx.permissions.add(principal_roles)
                elif isinstance(principal_roles, (list, set, tuple)):
                    ctx.permissions.update(principal_roles)
                principal_scopes = ctx.principal.get("scopes") or ctx.principal.get("scope") or []
                if isinstance(principal_scopes, str):
                    ctx.permissions.update(principal_scopes.split())
                elif isinstance(principal_scopes, (list, set, tuple)):
                    ctx.permissions.update(principal_scopes)

            try:
                return await self._execute_unified_pipeline(
                    ctx=ctx,
                    func=func,
                    args=args,
                    kwargs=kwargs,
                    tool_type=tool_type,
                    requires_approval=requires_approval,
                    idempotent=idempotent,
                    effective_timeout=timeout,
                    roles=roles,
                    scopes=scopes,
                )
            except AuthorizationError as e:
                raise HTTPException(status_code=403, detail=str(e))
            except HumanApprovalRequiredError as e:
                raise HTTPException(status_code=403, detail=str(e))
            except IdempotencyConflictError as e:
                raise HTTPException(status_code=409, detail=str(e))
            except RateLimitExceededError as e:
                raise HTTPException(status_code=429, detail=str(e))
            except PayloadTooLargeError as e:
                raise HTTPException(status_code=413, detail=str(e))
            except (ToolTimeoutError, TimeoutError) as e:
                raise HTTPException(status_code=504, detail=str(e))
            except SecurityConfigurationError as e:
                raise HTTPException(status_code=500, detail=str(e))
            except Exception:
                raise

        rest_wrapper.__signature__ = wrapper_sig
        return rest_wrapper

    # ─────────────────────────────────────────────
    # LIFESPAN INTEGRATION FOR MICROSERVICES
    # ─────────────────────────────────────────────
    def get_lifespan(self, external_lifespan: Callable[..., Any] | None = None):
        """
        Returns a composed lifespan asynccontextmanager that runs FastMCP's task group
        alongside any existing application lifespan (e.g. database connections).
        Reuses the exact same FastMCP ASGI instance as the mount.
        """
        mcp_app = self._get_or_create_mcp_app()

        @asynccontextmanager
        async def composed(app: FastAPI):
            await self.plugins.trigger_startup(self)
            try:
                async with mcp_app.lifespan(app):
                    if external_lifespan:
                        async with external_lifespan(app):
                            yield
                    else:
                        yield
            finally:
                await self.plugins.trigger_shutdown()

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
        mode: str | ExposureMode | None = None,
        expose: str | ExposureMode | None = None,
    ) -> FastAPI:
        """
        Mounts UnifiedNexus routes, routers, and FastMCP to an existing FastAPI application.
        Preserves existing app lifespans and prevents accidental REST exposure of MCP-only routers.
        """
        active_mode = normalize_exposure(expose or mode or self.mode)
        self.api = app

        clean_api_prefix = api_prefix.rstrip("/") if api_prefix else ""

        # 1. Register REST routes & routers if active_mode includes API
        if active_mode in (ExposureMode.BOTH, ExposureMode.API):
            for ep in self._endpoints:
                if ep["expose"] in (ExposureMode.BOTH, ExposureMode.API) and ep.get("path"):
                    clean_path = ep["path"] if ep["path"].startswith("/") else "/" + ep["path"]
                    full_path = f"{clean_api_prefix}{clean_path}" if clean_api_prefix else clean_path
                    target = ep["router"] if ep["router"] else app

                    dependencies = []
                    if self.rest_auth is not None:
                        dependencies.append(self.rest_auth)

                    target.add_api_route(
                        path=full_path,
                        endpoint=self._create_rest_endpoint_wrapper(ep),
                        methods=ep["methods"],
                        operation_id=ep["name"],
                        tags=ep["tags"],
                        summary=ep["summary"],
                        dependencies=dependencies or None,
                    )

            # Strictly include only routers configured for BOTH or API (NEVER MCP-only)
            for r in self._routers:
                if r["expose"] in (ExposureMode.BOTH, ExposureMode.API):
                    clean_r_prefix = r["prefix"] if r["prefix"].startswith("/") else ("/" + r["prefix"] if r["prefix"] else "")
                    full_r_prefix = f"{clean_api_prefix}{clean_r_prefix}" if clean_api_prefix else clean_r_prefix
                    dependencies = [self.rest_auth] if self.rest_auth is not None else None
                    app.include_router(
                        r["router"],
                        prefix=full_r_prefix,
                        tags=r["tags"],
                        dependencies=dependencies,
                    )

        # 2. Mount FastMCP and compose lifespan if active_mode includes MCP
        if active_mode in (ExposureMode.BOTH, ExposureMode.MCP):
            mcp_app = self._get_or_create_mcp_app(mcp_path)
            clean_mount = mcp_path.rstrip("/") if mcp_path and mcp_path != "/" else "/"

            original_lifespan = app.router.lifespan_context

            @asynccontextmanager
            async def composed_lifespan(target_app: FastAPI):
                await self.plugins.trigger_startup(self)
                try:
                    async with mcp_app.lifespan(target_app):
                        if original_lifespan:
                            async with original_lifespan(target_app):
                                yield
                        else:
                            yield
                finally:
                    await self.plugins.trigger_shutdown()

            app.router.lifespan_context = composed_lifespan
            app.mount(clean_mount, mcp_app)

        # 3. Attach middlewares
        for middleware_class, kwargs in self._middlewares:
            app.add_middleware(middleware_class, **kwargs)

        return app

    mount_to = attach_to
    mount = attach_to

    # ─────────────────────────────────────────────
    # FINALIZE
    # ─────────────────────────────────────────────
    def finalize(
        self,
        app: FastAPI | None = None,
        expose: str | ExposureMode | None = None,
        mode: str | ExposureMode | None = None,
        api_prefix: str = "",
        mcp_path: str = "/",
    ) -> FastAPI:
        """
        Builds the unified ASGI app (FastAPI + FastMCP) based on the exposure mode.
        If an existing app was provided, attaches to it.
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

        # 1. MCP app & Lifespan
        mcp_app = self._get_or_create_mcp_app(mcp_path) if active_mode in (ExposureMode.BOTH, ExposureMode.MCP) else None

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
                print("🌐  REST Docs  → http://localhost:8000/docs")
            if active_mode in (ExposureMode.BOTH, ExposureMode.MCP):
                print("🤖  MCP       → http://localhost:8000/mcp")
                print("🔍  Inspector → npx -y @modelcontextprotocol/inspector http://localhost:8000/mcp")
            print("-" * 60)

            await self.plugins.trigger_startup(self)
            try:
                if mcp_app:
                    async with mcp_app.lifespan(app_inst):
                        yield
                else:
                    yield
            finally:
                await self.plugins.trigger_shutdown()

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
                    dependencies = []
                    if self.rest_auth is not None:
                        dependencies.append(self.rest_auth)

                    target.add_api_route(
                        path=ep["path"],
                        endpoint=self._create_rest_endpoint_wrapper(ep),
                        methods=ep["methods"],
                        operation_id=ep["name"],
                        tags=ep["tags"],
                        summary=ep["summary"],
                        dependencies=dependencies or None,
                    )

            # Strictly include only routers configured for BOTH or API (NEVER MCP-only)
            for r in self._routers:
                if r["expose"] in (ExposureMode.BOTH, ExposureMode.API):
                    dependencies = [self.rest_auth] if self.rest_auth is not None else None
                    self.api.include_router(
                        r["router"],
                        prefix=r["prefix"],
                        tags=r["tags"],
                        dependencies=dependencies,
                    )

        # 5. Mount MCP if mode includes MCP
        if active_mode in (ExposureMode.BOTH, ExposureMode.MCP) and mcp_app:
            clean_mount = mcp_path.rstrip("/") if mcp_path and mcp_path != "/" else "/"
            self.api.mount(clean_mount, mcp_app)

        return self.api

    def run(
        self,
        transport: str = "http",
        host: str = "127.0.0.1",
        port: int = 8000,
        **kwargs,
    ):
        """Runs the server (stdio transport or HTTP via uvicorn)."""
        if transport == "stdio":
            self.mcp.run(transport="stdio", **kwargs)
        else:
            import uvicorn
            app = self.api or self.finalize()
            uvicorn.run(app, host=host, port=port, **kwargs)


__all__ = [
    "AuthorizationError",
    "ConcurrencyLimitError",
    "DuplicateRouteError",
    "DuplicateToolError",
    "ExposureMode",
    "HumanApprovalRequiredError",
    "NexusContext",
    "NexusPlugin",
    "PayloadTooLargeError",
    "PluginManager",
    "RateLimitExceededError",
    "SecurityConfigurationError",
    "ToolSafetyLevel",
    "ToolTimeoutError",
    "UnifiedNexus",
    "UnifiedNexusError",
    "normalize_exposure",
]
