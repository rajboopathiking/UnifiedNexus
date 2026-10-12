"""
Security and AI-Agent Safety utilities for UnifiedNexus.
Enforces parameter safety, tool classification, secret redaction, and bounded I/O.
"""

import asyncio
import hashlib
import inspect
import json
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from fastapi.params import Depends as DependsParam

try:
    from fastapi.params import Security as SecurityParam
except ImportError:
    SecurityParam = DependsParam  # Fallback if older FastAPI

from unified_nexus.exceptions import (
    AuthorizationError,
    IdempotencyConflictError,
    PayloadTooLargeError,
    SecurityConfigurationError,
)

# Reserved parameter names that must not be exposed as LLM tool arguments
SENSITIVE_PARAM_NAMES: set[str] = {
    "authorization",
    "auth",
    "token",
    "access_token",
    "api_key",
    "apikey",
    "secret",
    "client_secret",
    "password",
    "passwd",
    "session_id",
    "credentials",
    "current_user",
    "caller_role",
    "__approved__",
    "db",
    "db_session",
    "session",
}

# Regex patterns for sanitizing secrets from log messages and internal errors
RE_URL_CREDENTIALS = re.compile(
    r"((?:postgres|postgresql|mysql|mongodb|redis|amqp|mssql)://[^:]+:)([^@]+)(@)",
    re.IGNORECASE,
)
RE_AUTH_HEADERS = re.compile(
    r"(Bearer\s+)[A-Za-z0-9\-._~+/]+=*",
    re.IGNORECASE,
)
RE_SECRET_KVS = re.compile(
    r"((?:password|passwd|secret|api[_-]?key|access[_-]?token|auth|token)[\"']?\s*[:=]\s*[\"']?)([^\"'\s,;]+)",
    re.IGNORECASE,
)


def sanitize_message(msg: str) -> str:
    """Redacts passwords, tokens, API keys, and connection credentials from strings."""
    if not msg:
        return msg
    s = str(msg)
    s = RE_URL_CREDENTIALS.sub(r"\1***\3", s)
    s = RE_AUTH_HEADERS.sub(r"\1***", s)
    s = RE_SECRET_KVS.sub(r"\1***", s)
    return s


class ToolSafetyLevel(str, Enum):
    """Safety classification for LLM agent tools."""
    READ_ONLY = "read_only"       # Safe, idempotent query (e.g. search, get status)
    ACTION = "action"             # Standard state modification (e.g. create record, send email)
    DESTRUCTIVE = "destructive"   # Potentially irreversible change (e.g. delete, refund, wipe)


def validate_mcp_parameters(tool_name: str, sig_parameters: dict) -> None:
    """
    Validates that a function's parameters are safe for MCP registration.
    Rejects FastAPI Depends/Security and sensitive credentials in signatures.
    """
    for p_name, param in sig_parameters.items():
        # 1. Reject FastAPI Depends and Security defaults
        if isinstance(param.default, (DependsParam, SecurityParam)):
            raise SecurityConfigurationError(
                f"Cannot register endpoint '{tool_name}' to MCP: Parameter '{p_name}' "
                f"uses FastAPI dependency injection ({type(param.default).__name__}). "
                f"FastAPI dependencies cannot be invoked safely in MCP. "
                f"Use expose='api' for REST-only dependency endpoints, or extract context "
                f"using FastMCP authentication/context."
            )

        # 2. Reject sensitive authentication parameters from LLM exposure
        if p_name.lower() in SENSITIVE_PARAM_NAMES:
            raise SecurityConfigurationError(
                f"Cannot register endpoint '{tool_name}' to MCP: Parameter '{p_name}' "
                f"is a sensitive credential parameter. Request credentials and user identity "
                f"must not be exposed as LLM tool arguments."
            )


def check_input_bounds(kwargs: dict[str, Any], max_chars: int) -> None:
    """Ensures input strings do not exceed max allowed characters."""
    for key, val in kwargs.items():
        if isinstance(val, str) and len(val) > max_chars:
            raise PayloadTooLargeError(
                f"Input argument '{key}' length ({len(val)}) exceeds maximum allowed "
                f"size ({max_chars} characters)."
            )


def bound_output(result: Any, max_chars: int) -> Any:
    """Bounds tool output size to prevent LLM context exhaustion."""
    if isinstance(result, str):
        if len(result) > max_chars:
            return result[:max_chars] + f"\n... [Output truncated: exceeded {max_chars} characters limit]"
        return result

    try:
        # Check formatted string/JSON size
        res_str = json.dumps(result, default=str)
        if len(res_str) > max_chars:
            truncated = res_str[:max_chars]
            return {
                "result": truncated,
                "_truncated": True,
                "warning": f"Output exceeded {max_chars} characters and was truncated for safety.",
            }
    except Exception:
        pass

    return result


def compute_payload_hash(kwargs: dict[str, Any]) -> str:
    """
    Computes a deterministic SHA-256 hash of tool arguments.
    Excludes the idempotency key itself and framework-injected Request objects,
    recursing into Pydantic models when present.
    """
    ignored_keys = {"idempotency_key", "idempotent_key", "request"}
    filtered: dict[str, Any] = {}
    for k, v in kwargs.items():
        if k in ignored_keys:
            continue
        if hasattr(v, "model_dump") and callable(v.model_dump):
            dumped = v.model_dump()
            filtered[k] = {dk: dv for dk, dv in dumped.items() if dk not in ignored_keys}
        elif hasattr(v, "dict") and callable(v.dict):
            dumped = v.dict()
            filtered[k] = {dk: dv for dk, dv in dumped.items() if dk not in ignored_keys}
        else:
            filtered[k] = v

    try:
        serialized = json.dumps(filtered, sort_keys=True, default=str)
    except Exception:
        serialized = str(sorted((str(k), str(v)) for k, v in filtered.items()))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def compute_caller_identity(principal: Any, require_authenticated: bool = False) -> str:
    """
    Computes a canonical, delimiter-safe string representation of verified caller identity/tenant.
    Ensures idempotency keys are strictly isolated per tenant/user without delimiter collisions.
    If require_authenticated is True and principal is None, raises AuthorizationError.
    """
    if principal is None:
        if require_authenticated:
            raise AuthorizationError(
                "Unauthorized: Authenticated identity required for privileged operation."
            )
        return "anonymous"
    if isinstance(principal, (int, float, bool)):
        return str(principal)
    if isinstance(principal, str):
        # Escape colons to prevent delimiter collisions
        return urllib.parse.quote(principal, safe="")
    if isinstance(principal, dict):
        tenant = principal.get("tenant_id") or principal.get("org_id")
        user = principal.get("user_id") or principal.get("sub") or principal.get("username")
        if tenant is not None and user is not None:
            # Unambiguously percent-encode components to prevent ':user:' delimiter collisions
            safe_tenant = urllib.parse.quote(str(tenant), safe="")
            safe_user = urllib.parse.quote(str(user), safe="")
            return f"tenant:{safe_tenant}:user:{safe_user}"
        try:
            return json.dumps(principal, sort_keys=True, separators=(",", ":"), default=str)
        except Exception:
            return urllib.parse.quote(str(principal), safe="")
    if hasattr(principal, "__dict__"):
        try:
            return json.dumps(principal.__dict__, sort_keys=True, separators=(",", ":"), default=str)
        except Exception:
            pass
    return urllib.parse.quote(str(principal), safe="")


@runtime_checkable
class BaseIdempotencyBackend(Protocol):
    """Protocol for idempotency storage backends (in-memory or distributed)."""

    async def acquire_or_get(
        self,
        key: str,
        payload_hash: str,
        wait_timeout: float = 30.0,
        owner_token: str | None = None,
        lock_timeout: float | None = None,
    ) -> tuple[str, Any]:
        """
        Atomically inspects and reserves an idempotency key.
        Returns:
            ("CACHED", result): Key completed; returns cached result without execution.
            ("ACQUIRED", None): Key reserved; caller must proceed with execution.
            ("CONFLICT", None): Key exists but payload_hash does not match (tampering / collision).
            ("TIMEOUT", None): Timed out waiting for an in-flight concurrent execution.
        """
        ...

    async def complete(
        self,
        key: str,
        payload_hash: str,
        result: Any,
        owner_token: str | None = None,
    ) -> None:
        """Marks key as completed and stores the result."""
        ...

    async def release(
        self,
        key: str,
        owner_token: str | None = None,
    ) -> None:
        """Releases a pending reservation if tool execution fails."""
        ...


@dataclass
class IdempotencyRecord:
    status: str  # "PENDING" | "COMPLETED"
    payload_hash: str
    created_at: float
    result: Any = None
    event: asyncio.Event = field(default_factory=asyncio.Event)
    owner_token: str | None = None
    lock_timeout: float = 60.0


class IdempotencyCache:
    """
    Thread-safe and async-safe in-memory cache for deduplicating idempotent operations.
    Enforces atomic in-flight locking, tenant/identity binding, payload hash validation,
    bounded waiter rechecking, and automatic TTL/lock_timeout expiration.
    """

    def __init__(
        self,
        maxsize: int = 1000,
        ttl_seconds: float = 3600.0,
        lock_timeout: float = 60.0,
    ):
        self._cache: dict[str, IdempotencyRecord] = {}
        self._maxsize = maxsize
        self._ttl = ttl_seconds
        self._lock_timeout = lock_timeout
        self._lock = asyncio.Lock()

    def _prune_expired_or_overflow(self) -> None:
        now = time.time()
        # Remove expired completed and expired pending entries
        expired_keys = []
        for k, rec in self._cache.items():
            if rec.status == "COMPLETED" and (now - rec.created_at) > self._ttl:
                expired_keys.append(k)
            elif rec.status == "PENDING" and (now - rec.created_at) > rec.lock_timeout:
                rec.event.set()
                expired_keys.append(k)
        for k in expired_keys:
            del self._cache[k]

        # If still over maxsize, evict oldest COMPLETED entry
        if len(self._cache) >= self._maxsize:
            completed_keys = [
                k for k, rec in self._cache.items()
                if rec.status == "COMPLETED"
            ]
            if completed_keys:
                oldest_key = min(completed_keys, key=lambda k: self._cache[k].created_at)
                del self._cache[oldest_key]

    async def acquire_or_get(
        self,
        key: str,
        payload_hash: str,
        wait_timeout: float = 30.0,
        owner_token: str | None = None,
        lock_timeout: float | None = None,
    ) -> tuple[str, Any]:
        """
        Atomically reserves an idempotency key or retrieves the cached result.
        Returns ("CACHED", result), ("ACQUIRED", None), ("CONFLICT", None), or ("TIMEOUT", None).
        Guarantees that multiple waiting callers do NOT all receive ACQUIRED when a pending key
        is released or abandoned. Only one waiter acquires; other waiters wait on the replacement reservation.
        """
        deadline = time.time() + wait_timeout
        effective_lock_timeout = float(lock_timeout if lock_timeout is not None else self._lock_timeout)

        while True:
            async with self._lock:
                now = time.time()
                rec = self._cache.get(key)

                # Check if COMPLETED record expired
                if rec and rec.status == "COMPLETED" and (now - rec.created_at > self._ttl):
                    del self._cache[key]
                    rec = None

                # Check if PENDING record expired (lock_timeout exceeded)
                if rec and rec.status == "PENDING" and (now - rec.created_at > rec.lock_timeout):
                    rec.event.set()
                    del self._cache[key]
                    rec = None

                if rec is None:
                    # Key is available: atomically reserve reservation
                    self._prune_expired_or_overflow()
                    event = asyncio.Event()
                    self._cache[key] = IdempotencyRecord(
                        status="PENDING",
                        payload_hash=payload_hash,
                        created_at=now,
                        event=event,
                        owner_token=owner_token,
                        lock_timeout=effective_lock_timeout,
                    )
                    return "ACQUIRED", None

                # Record exists: verify payload hash to prevent key reuse with different parameters
                if rec.payload_hash != payload_hash:
                    return "CONFLICT", None

                if rec.status == "COMPLETED":
                    return "CACHED", rec.result

                # Record is PENDING (another concurrent request with identical payload is running)
                event = rec.event

            # Wait for the in-flight operation outside the lock
            remaining = deadline - time.time()
            if remaining <= 0:
                return "TIMEOUT", None

            try:
                await asyncio.wait_for(event.wait(), timeout=remaining)
            except (TimeoutError, asyncio.TimeoutError):
                return "TIMEOUT", None

            # When event fires (completed, released, or expired), loop repeats:
            # - If completed: returns ("CACHED", result) under lock
            # - If released: FIRST waiter entering lock acquires ("ACQUIRED", None) and creates a NEW event
            # - Subsequent waiters entering lock see status="PENDING" with the new event and wait on it

    async def complete(
        self,
        key: str,
        payload_hash: str,
        result: Any,
        owner_token: str | None = None,
    ) -> None:
        """Stores the successful result and notifies any awaiting concurrent requests."""
        async with self._lock:
            rec = self._cache.get(key)
            if rec:
                # Do not overwrite if key is already COMPLETED by another owner
                if rec.status == "COMPLETED":
                    return
                # Do not overwrite if key was claimed by another owner after timeout
                if owner_token is not None and rec.owner_token is not None and rec.owner_token != owner_token:
                    return
                rec.status = "COMPLETED"
                rec.result = result
                rec.payload_hash = payload_hash
                rec.created_at = time.time()
                rec.owner_token = owner_token
                rec.event.set()

    async def release(self, key: str, owner_token: str | None = None) -> None:
        """Releases a pending reservation if tool execution fails."""
        async with self._lock:
            rec = self._cache.get(key)
            if rec and rec.status == "PENDING":
                if owner_token is None or rec.owner_token is None or rec.owner_token == owner_token:
                    rec.event.set()
                    del self._cache[key]

    async def get(self, key: str) -> tuple[bool, Any]:
        """Legacy compatibility lookup."""
        async with self._lock:
            rec = self._cache.get(key)
            if not rec or rec.status != "COMPLETED":
                return False, None
            if time.time() - rec.created_at > self._ttl:
                del self._cache[key]
                return False, None
            return True, rec.result

    async def set(self, key: str, value: Any, payload_hash: str = "") -> None:
        """Legacy compatibility setter."""
        async with self._lock:
            self._prune_expired_or_overflow()
            event = asyncio.Event()
            event.set()
            self._cache[key] = IdempotencyRecord(
                status="COMPLETED",
                payload_hash=payload_hash,
                created_at=time.time(),
                result=value,
                event=event,
            )

    def clear(self) -> None:
        self._cache.clear()


class RedisIdempotencyBackend:
    """
    Distributed Redis-backed storage for idempotency deduplication with atomic Lua scripting.
    Adheres to BaseIdempotencyBackend protocol.
    Supports multi-worker Uvicorn / Gunicorn clusters with atomic locking (SET NX EX),
    deterministic SHA-256 payload conflict detection, automatic TTL expiration,
    and atomic Lua ownership validation preventing stale deletions and overwrites.
    """

    LUA_RELEASE = """
local raw = redis.call('GET', KEYS[1])
if not raw then return 0 end
local ok, rec = pcall(cjson.decode, raw)
if not ok or type(rec) ~= 'table' then return 0 end
if rec.status == 'PENDING' then
    local expected = ARGV[1]
    if not expected or expected == '' or rec.owner_token == expected then
        return redis.call('DEL', KEYS[1])
    end
end
return 0
"""

    LUA_COMPLETE = """
local raw = redis.call('GET', KEYS[1])
if not raw then return 0 end
local ok, rec = pcall(cjson.decode, raw)
if not ok or type(rec) ~= 'table' then return 0 end
if rec.status == 'PENDING' then
    local expected = ARGV[1]
    if not expected or expected == '' or rec.owner_token == expected then
        redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3])
        return 1
    end
end
return 0
"""

    def __init__(
        self,
        redis_client: Any = None,
        redis_url: str | None = None,
        key_prefix: str = "nexus:idempotency:",
        ttl_seconds: float = 3600.0,
        lock_timeout: float = 60.0,
        poll_interval: float = 0.05,
    ):
        self._client = redis_client
        self._redis_url = redis_url
        self._key_prefix = key_prefix
        self._ttl = ttl_seconds
        self._lock_timeout = lock_timeout
        self._poll_interval = poll_interval

    async def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        if self._redis_url:
            try:
                import redis.asyncio as aioredis  # type: ignore

                self._client = aioredis.from_url(self._redis_url)
                return self._client
            except ImportError as e:
                raise ImportError(
                    "The 'redis' package is required to connect to redis_url. "
                    "Install it via 'pip install redis'."
                ) from e
        raise ValueError(
            "RedisIdempotencyBackend requires either 'redis_client' or 'redis_url' to be configured."
        )

    async def acquire_or_get(
        self,
        key: str,
        payload_hash: str,
        wait_timeout: float = 30.0,
        owner_token: str | None = None,
        lock_timeout: float | None = None,
    ) -> tuple[str, Any]:
        """
        Atomically inspects and reserves an idempotency key in Redis.
        Returns:
            ("CACHED", result): Key was previously completed; returns cached result.
            ("ACQUIRED", None): Key reserved; caller must proceed with execution.
            ("CONFLICT", None): Key exists with differing payload_hash.
            ("TIMEOUT", None): Timed out waiting for an in-flight operation.
        """
        client = await self._get_client()
        full_key = f"{self._key_prefix}{key}"
        now = time.time()
        deadline = now + wait_timeout
        effective_lock_timeout_sec = float(lock_timeout if lock_timeout is not None else self._lock_timeout)
        effective_lock_timeout_ms = max(int(effective_lock_timeout_sec * 1000), 1)

        pending_data = json.dumps({
            "status": "PENDING",
            "payload_hash": payload_hash,
            "created_at": now,
            "owner_token": owner_token,
        })

        # Atomic reservation via SET NX PX (millisecond precision)
        acquired = await client.set(full_key, pending_data, nx=True, px=effective_lock_timeout_ms)
        if acquired:
            return "ACQUIRED", None

        # Key already exists: inspect status or poll for completion
        while True:
            raw = await client.get(full_key)
            if not raw:
                # Key was deleted (e.g. concurrent execution failed/released lock or expired)
                acquired = await client.set(full_key, pending_data, nx=True, px=effective_lock_timeout_ms)
                if acquired:
                    return "ACQUIRED", None
                await asyncio.sleep(self._poll_interval)
                if time.time() >= deadline:
                    return "TIMEOUT", None
                continue

            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")

            try:
                record = json.loads(raw)
            except Exception:
                return "CONFLICT", None

            existing_hash = record.get("payload_hash")
            if existing_hash and existing_hash != payload_hash:
                return "CONFLICT", None

            status = record.get("status")
            if status == "COMPLETED":
                return "CACHED", record.get("result")

            # Still PENDING: wait for completion
            if time.time() >= deadline:
                return "TIMEOUT", None

            await asyncio.sleep(self._poll_interval)

    async def complete(
        self,
        key: str,
        payload_hash: str,
        result: Any,
        owner_token: str | None = None,
    ) -> None:
        """
        Stores completed result with full TTL in Redis.
        Validates lock ownership atomically via Lua script:
        - Prevents stale completion when another owner's record is already COMPLETED.
        - Prevents stale completion when key is PENDING under another owner's token.
        - Prevents writing if key expired.
        """
        client = await self._get_client()
        full_key = f"{self._key_prefix}{key}"
        token_arg = owner_token or ""
        completed_data = json.dumps({
            "status": "COMPLETED",
            "payload_hash": payload_hash,
            "result": result,
            "owner_token": owner_token,
            "created_at": time.time(),
        }, default=str)
        ttl_arg = int(self._ttl)

        if not hasattr(client, "eval"):
            raise SecurityConfigurationError(
                "RedisIdempotencyBackend requires a Redis client supporting atomic Lua scripting ('eval'). "
                "Non-atomic fallbacks are forbidden in production to prevent lock ownership races."
            )

        # Atomic Lua execution - propagate any server or connection exceptions fail-closed
        await client.eval(self.LUA_COMPLETE, 1, full_key, token_arg, completed_data, ttl_arg)

    async def release(self, key: str, owner_token: str | None = None) -> None:
        """
        Deletes pending reservation key if tool execution failed.
        Guarantees lock ownership atomically via Lua script:
        - Only deletes if the lock still belongs to owner_token and is PENDING.
        - Never deletes another worker's new lock or a COMPLETED result.
        Fail-closed: requires atomic eval and propagates Lua execution failures without
        falling back to non-atomic GET/DEL races.
        """
        client = await self._get_client()
        full_key = f"{self._key_prefix}{key}"
        token_arg = owner_token or ""

        if not hasattr(client, "eval"):
            raise SecurityConfigurationError(
                "RedisIdempotencyBackend requires a Redis client supporting atomic Lua scripting ('eval'). "
                "Non-atomic fallbacks are forbidden in production to prevent lock ownership races."
            )

        # Atomic Lua execution - propagate any server or connection exceptions fail-closed
        await client.eval(self.LUA_RELEASE, 1, full_key, token_arg)

    async def close(self) -> None:
        """Closes the underlying Redis connection if supported."""
        if self._client is not None and hasattr(self._client, "aclose"):
            await self._client.aclose()
        elif self._client is not None and hasattr(self._client, "close"):
            res = self._client.close()
            if inspect.iscoroutine(res):
                await res


