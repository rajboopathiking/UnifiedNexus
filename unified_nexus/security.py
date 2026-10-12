"""
Security and AI-Agent Safety utilities for UnifiedNexus.
Enforces parameter safety, tool classification, secret redaction, and bounded I/O.
"""

import asyncio
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from fastapi.params import Depends as DependsParam

try:
    from fastapi.params import Security as SecurityParam
except ImportError:
    SecurityParam = DependsParam  # Fallback if older FastAPI

from unified_nexus.exceptions import (
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


def compute_caller_identity(principal: Any) -> str:
    """
    Computes a stable string representation of verified caller identity/tenant.
    Ensures idempotency keys are strictly isolated per tenant/user.
    """
    if principal is None:
        return "anonymous"
    if isinstance(principal, (str, int, float, bool)):
        return str(principal)
    if isinstance(principal, dict):
        tenant = principal.get("tenant_id") or principal.get("org_id")
        user = principal.get("user_id") or principal.get("sub") or principal.get("username")
        if tenant and user:
            return f"tenant:{tenant}:user:{user}"
        try:
            return json.dumps(principal, sort_keys=True)
        except Exception:
            return str(principal)
    return str(principal)


@runtime_checkable
class BaseIdempotencyBackend(Protocol):
    """Protocol for idempotency storage backends (in-memory or distributed)."""

    async def acquire_or_get(
        self, key: str, payload_hash: str, wait_timeout: float = 30.0
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

    async def complete(self, key: str, payload_hash: str, result: Any) -> None:
        """Marks key as completed and stores the result."""
        ...

    async def release(self, key: str) -> None:
        """Releases a pending reservation if tool execution fails."""
        ...


@dataclass
class IdempotencyRecord:
    status: str  # "PENDING" | "COMPLETED"
    payload_hash: str
    created_at: float
    result: Any = None
    event: asyncio.Event = field(default_factory=asyncio.Event)


class IdempotencyCache:
    """
    Thread-safe and async-safe in-memory cache for deduplicating idempotent operations.
    Enforces atomic in-flight locking, tenant/identity binding, payload hash validation,
    and automatic TTL expiration.
    """

    def __init__(self, maxsize: int = 1000, ttl_seconds: float = 3600.0):
        self._cache: dict[str, IdempotencyRecord] = {}
        self._maxsize = maxsize
        self._ttl = ttl_seconds
        self._lock = asyncio.Lock()

    def _prune_expired_or_overflow(self) -> None:
        now = time.time()
        # Remove expired entries
        expired_keys = [
            k for k, rec in self._cache.items()
            if (now - rec.created_at) > self._ttl and rec.status == "COMPLETED"
        ]
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
        self, key: str, payload_hash: str, wait_timeout: float = 30.0
    ) -> tuple[str, Any]:
        """
        Atomically reserves an idempotency key or retrieves the cached result.
        Returns ("CACHED", result), ("ACQUIRED", None), ("CONFLICT", None), or ("TIMEOUT", None).
        """
        async with self._lock:
            now = time.time()
            record = self._cache.get(key)

            # Check if existing record is expired
            if record and (now - record.created_at > self._ttl) and record.status == "COMPLETED":
                del self._cache[key]
                record = None

            if record is None:
                self._prune_expired_or_overflow()
                event = asyncio.Event()
                self._cache[key] = IdempotencyRecord(
                    status="PENDING",
                    payload_hash=payload_hash,
                    created_at=now,
                    event=event,
                )
                return "ACQUIRED", None

            # Record exists: verify payload hash to prevent key reuse with different parameters
            if record.payload_hash != payload_hash:
                return "CONFLICT", None

            if record.status == "COMPLETED":
                return "CACHED", record.result

            # Record is PENDING (another concurrent request with identical payload is running)
            event = record.event

        # Wait for the in-flight operation to complete outside the lock
        try:
            await asyncio.wait_for(event.wait(), timeout=wait_timeout)
        except (TimeoutError, asyncio.TimeoutError):
            return "TIMEOUT", None

        # Re-check under lock
        async with self._lock:
            rec = self._cache.get(key)
            if rec and rec.status == "COMPLETED":
                if rec.payload_hash != payload_hash:
                    return "CONFLICT", None
                return "CACHED", rec.result

            # If the concurrent operation failed or cleared without completing, acquire for this caller
            event = asyncio.Event()
            self._cache[key] = IdempotencyRecord(
                status="PENDING",
                payload_hash=payload_hash,
                created_at=time.time(),
                event=event,
            )
            return "ACQUIRED", None

    async def complete(self, key: str, payload_hash: str, result: Any) -> None:
        """Stores the successful result and notifies any awaiting concurrent requests."""
        async with self._lock:
            rec = self._cache.get(key)
            if rec:
                rec.status = "COMPLETED"
                rec.result = result
                rec.payload_hash = payload_hash
                rec.created_at = time.time()
                rec.event.set()

    async def release(self, key: str) -> None:
        """Releases a pending reservation if tool execution fails."""
        async with self._lock:
            rec = self._cache.get(key)
            if rec and rec.status == "PENDING":
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

