"""
UnifiedNexus: One decorator. Two interfaces.
Bridges FastAPI and Model Context Protocol (MCP) with enterprise-grade security
and an extensible plugin architecture.
"""

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
    RedisIdempotencyBackend,
)
from unified_nexus.unified_nexus import (
    ExposureMode,
    ToolSafetyLevel,
    UnifiedNexus,
    normalize_exposure,
)

__all__ = [
    "AuthorizationError",
    "BaseIdempotencyBackend",
    "ConcurrencyLimitError",
    "DuplicateRouteError",
    "DuplicateToolError",
    "ExposureMode",
    "HumanApprovalRequiredError",
    "IdempotencyCache",
    "IdempotencyConflictError",
    "NexusContext",
    "NexusPlugin",
    "PayloadTooLargeError",
    "PluginManager",
    "RateLimitExceededError",
    "RedisIdempotencyBackend",
    "SecurityConfigurationError",
    "ToolSafetyLevel",
    "ToolTimeoutError",
    "UnifiedNexus",
    "UnifiedNexusError",
    "normalize_exposure",
    "__version__",
]

__version__ = "0.2.0"
