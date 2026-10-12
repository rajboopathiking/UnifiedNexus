# Changelog

All notable changes to **Unified Nexus** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [0.2.0] - 2026-10-12

### Added
- **Unified Execution Pipeline**:
  - Centralized `_execute_unified_pipeline()` for identical execution semantics across REST and MCP transports.
  - Strict policy parity: timeout cancellation, concurrency semaphores, rate limiting, bounded input/output, and human approval verification enforced equally across both transports.
- **Fail-Closed Authorization & Anti-Spoofing**:
  - Complete elimination of caller-controlled role arguments (`role`, `caller_role`, `user_id`, `principal`) from authorization decisions. Caller identity is exclusively derived from verified token context (`ctx.principal`, `ctx.permissions`) or an application `authorize_hook`.
  - Native scope enforcement: declared `scopes` on tools are strictly verified against `ctx.permissions` fail-closed (`AuthorizationError`) even when no application `authorize_hook` is registered.
  - Privileged identity rejection: tools declared with `roles`, `scopes`, `requires_approval`, or `ToolSafetyLevel.DESTRUCTIVE` reject unauthenticated callers (`ctx.principal is None`) rather than falling back to the shared `"anonymous"` partition.
  - Automatic extraction of FastMCP authenticated token claims (`subject`, `client_id`, `scopes`, `roles`) and FastAPI `request.state.user` / `request.state.permissions` into `NexusContext`.
  - Blocked `__approved__=True` backdoor; destructive actions unconditionally require a registered application `approval_hook`.
  - Rate limiting hooks fail closed (`RateLimitExceededError`) on any hook exceptions.
  - Router-level security dependencies (`rest_auth`) automatically propagate to included routers in both `attach_to()` and `finalize()`.
- **Multi-Tenant & Atomic Idempotency Deduplication**:
  - Strict post-authorization execution: idempotency cache is never queried before rate limiting, authorization, and human approvals pass.
  - Tenant and user identity partitioning: cache keys are scoped to caller (`caller_id:tool_name:idem_key`), preventing cross-user data leakage.
  - Cryptographic payload binding: SHA-256 hash validation of arguments detects parameter tampering and raises `IdempotencyConflictError` (HTTP 409 / ToolError).
  - Atomic in-flight concurrency reservation: concurrent duplicate requests await the active execution and return the cached response, ensuring side effects run **exactly once**.
    - `BaseIdempotencyBackend` protocol and built-in `RedisIdempotencyBackend` providing distributed multi-worker support (Uvicorn / Gunicorn clusters) with atomic `SET NX EX` locking alongside the in-memory `IdempotencyCache`.
    - **Lock Ownership Tokens & Crash Recovery**: Each reservation generates a cryptographic `owner_token`. Lock completion and release strictly validate token ownership, preventing expired locks claimed by a new worker from being overwritten or prematurely deleted by stale worker processes.
    - **Side-Effect Recovery on Timeout**: When side-effecting operations (`ACTION` or `DESTRUCTIVE`) encounter timeouts, the idempotency lock is retained rather than prematurely released, preventing concurrent agent retry storms during background thread recovery.
    - **Durable Idempotency Key**: `NexusContext` exposes `ctx.idempotency_key` and qualified `ctx.durable_idempotency_key` (`caller_id:tool_name:idem_key`) to propagate directly to downstream business services (e.g. Stripe, database ledger) for end-to-end transactional safety.
- **Plugin Architecture & Lifecycle Hooks**:
  - `NexusPlugin` protocol with `startup()`, `before_execute()`, `after_execute()`, `on_error()`, and `shutdown()`.
  - `nexus.add_plugin(plugin)` for intercepting both REST and MCP requests with a single plugin.
  - `NexusContext` capturing transport (`'rest'` | `'mcp'`), request/correlation ID, tool name, verified caller principal, permissions, duration, and idempotency keys.
- **Enterprise Security & Boundaries**:
  - Rejection of unsupported FastAPI `Depends()` and `Security()` defaults in MCP tools via `SecurityConfigurationError`.
  - Blocked sensitive credential parameters (`password`, `token`, `secret`, `api_key`) from LLM tool argument exposure.
  - Rejection of routers with router-level security dependencies from silent/unauthenticated MCP exposure.
  - Duplicate tool name detection (`DuplicateToolError`) and duplicate REST route detection (`DuplicateRouteError`).
  - Automatic secret and credential redaction from internal exception traces and logs.
  - Dedicated `rest_auth` parameter on `UnifiedNexus` for REST API boundary security.
  - Native FastMCP error signaling via `ToolError` (ensuring `isError: true` JSON-RPC response).
  - Dynamic Starlette `Request` injection into REST route wrappers ensuring caller `request.state` (authenticated user and scopes) is seamlessly bound even when handlers do not declare `request: Request`.
- **AI-Agent Safety Invariants**:
  - `ToolSafetyLevel` classification (`READ_ONLY`, `ACTION`, `DESTRUCTIVE`).
  - Human approval hooks (`approval_hook`) and per-tool `requires_approval` flags for destructive operations.
  - Idempotency declaration (`idempotent=True`) for safe AI agent retries on side-effecting operations.
  - Bounded input string sizes (`max_input_chars`) and safe output truncation (`max_output_chars`).
  - Concurrency limiting via `asyncio.Semaphore` and configurable per-tool timeouts.
  - Non-blocking execution of synchronous tools in Starlette's threadpool (`run_in_threadpool`) with startup warnings for synchronous `DESTRUCTIVE` tools and runtime warnings on sync timeout.
- **MCP Transport Normalization**:
  - Normalization of external mount paths eliminating `/mcp/mcp` duplication across both `/` and `/mcp` mounts.
  - Strict preservation of parent FastAPI lifespans (executing exactly once on startup and shutdown).
  - Single ASGI sub-application reuse across both mounting and lifespan context management.
- **Testing & Quality**:
  - 63 automated unit, integration, security regression, and real-client operational tests passing with 100% pass rate.
  - Real authenticated REST client testing with actual Bearer tokens and scope enforcement.
  - Real authenticated MCP client testing with FastMCP `TokenVerifier` and `AccessToken` claims extraction.
  - Multi-worker Redis lock ownership, expiration, and crash recovery test suite.
  - Side-effect recovery on timeout and durable idempotency key propagation test suite.
  - Full GitHub Actions CI workflow (`.github/workflows/ci.yml`) testing Python 3.11 and 3.12, linting with Ruff, building distribution, and validating wheel install.
  - End-to-end AI Customer Support Agent proof-of-concept testing three realistic scenarios across REST and MCP.

---

## [0.1.3] - 2026-10-12

### Added
- **Exposure Control**:
  - Application-level exposure modes: `'both'` (default), `'api'` / `'api_only'`, and `'mcp'` / `'mcp_only'`.
  - Tool-level exposure modes via `expose="both"`, `expose="api"`, and `expose="mcp"`.
  - Convenient decorator shortcuts: `@nexus.api_only(path=...)` and `@nexus.mcp_only`.
  - Dynamic exposure mode override in `nexus.finalize(expose=...)`.
  - Synchronized `nexus.mode` and `nexus.expose` property accessors.
- **FastAPI Microservice Integration**:
  - `nexus.attach_to(app)` and `nexus.mount(app)` methods for mounting UnifiedNexus onto existing FastAPI microservice applications.
  - Automatic lifespan composition combining existing database/telemetry lifespans with FastMCP's task group.
  - Standalone lifespan helper `nexus.get_lifespan()` / `nexus.combine_lifespan()`.
  - `api_prefix` support (e.g. `/api/v1`) for versioned REST API routing.
  - In-process automatic resolution of FastAPI `Depends(...)` when functions are executed by AI agents via MCP.
  - Router auto-introspection via `nexus.include_router(router, expose="both")` to expose entire existing APIRouters to MCP tools.
  - Multi-transport server execution via `nexus.run(transport="http"|"stdio")`.
- **LangServe Collaboration**:
  - Full interoperability running LangChain LCEL chains via LangServe alongside FastMCP tools on a single FastAPI app.
  - In-process execution of LangChain chains from within FastMCP tools.
  - Dedicated example `08_langserve_collaboration.py`.
- **PyPI & Production Packaging**:
  - Structured `unified_nexus` package directory with PEP 561 `py.typed` type annotations marker.
  - Clean Hatchling wheel target building production distributions.
- **Examples**:
  - Added dedicated runnable examples covering all core features in `examples/`:
    - `01_quickstart.py`: Basic universal tool, auto method detection.
    - `02_exposure_modes.py`: App & tool level exposure controls.
    - `03_router_integration.py`: Modular architecture & router auto-introspection.
    - `04_middleware_and_security.py`: CORS, custom middlewares, error recovery.
    - `05_dependency_injection.py`: FastAPI `Depends()` resolution in MCP.
    - `06_microservice_mount.py`: Attaching to production FastAPI microservices.
    - `07_stdio_agent_server.py`: Local stdio MCP server for Claude Desktop / Cursor CLI.
    - `08_langserve_collaboration.py`: LangServe LCEL chain + FastMCP tools in one FastAPI app.
    - `microservice_example.py`: Production order microservice with health probes.
- **Testing**:
  - Added 24 comprehensive tests covering exposure modes, microservice mounting, dependency injection, LangServe collaboration, and advanced features.


---

## [0.1.2] - 2026-03-04

### Added
- Support for APIRouter attachments via `@nexus.universal_tool(router=...)`.
- `nexus.include_router()` method.
- FastMCP lifespan integration.

---

## [0.1.0] - 2026-03-01

### Added
- Initial release of Unified Nexus.
- `@nexus.universal_tool` decorator for unified FastAPI and FastMCP endpoints.
- Auto HTTP method detection (`GET` vs `POST`).
- Pydantic schema serialization for LLM tools.
