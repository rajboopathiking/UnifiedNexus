"""
UnifiedNexus Exception Hierarchy.
Clear, actionable exceptions for security, routing, and execution failures.
"""


class UnifiedNexusError(Exception):
    """Base exception for all UnifiedNexus errors."""


class SecurityConfigurationError(UnifiedNexusError):
    """
    Raised when an insecure configuration is detected during registration.
    Examples:
    - Attempting to expose FastAPI Depends/Security dependencies to MCP without safe resolution.
    - Attempting to expose routers with router-level security dependencies to MCP.
    - Exposing sensitive credential parameters as LLM tool arguments.
    """


class DuplicateToolError(UnifiedNexusError):
    """Raised when an MCP tool with an existing name is registered."""


class DuplicateRouteError(UnifiedNexusError):
    """Raised when a REST route with an existing (method, path) combination is registered."""


class AuthorizationError(UnifiedNexusError):
    """Raised when a tool caller fails execution-time authorization checks."""


class HumanApprovalRequiredError(UnifiedNexusError):
    """Raised when a destructive or action tool requires human confirmation before execution."""


class ToolTimeoutError(UnifiedNexusError):
    """Raised when tool execution exceeds the configured timeout."""


class ConcurrencyLimitError(UnifiedNexusError):
    """Raised when tool execution exceeds the maximum concurrency limit."""


class PayloadTooLargeError(UnifiedNexusError):
    """Raised when tool input or output exceeds configured size bounds."""


class RateLimitExceededError(UnifiedNexusError):
    """Raised when tool invocation exceeds the configured rate limit."""


class IdempotencyConflictError(UnifiedNexusError):
    """Raised when an idempotency key is reused with differing request parameters or concurrent conflict occurs."""
