"""
Security and AI-Agent Safety utilities for UnifiedNexus.
Enforces parameter safety, tool classification, secret redaction, and bounded I/O.
"""

import json
import re
from enum import Enum
from typing import Any

from fastapi.params import Depends as DependsParam

try:
    from fastapi.params import Security as SecurityParam
except ImportError:
    SecurityParam = DependsParam  # Fallback if older FastAPI

from unified_nexus.exceptions import (
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
