"""
Root forwarding module for UnifiedNexus.
Directs imports to the unified_nexus package.
"""

from unified_nexus import __version__
from unified_nexus.context import NexusContext
from unified_nexus.exceptions import (
    AuthorizationError,
    ConcurrencyLimitError,
    DuplicateRouteError,
    DuplicateToolError,
    HumanApprovalRequiredError,
    PayloadTooLargeError,
    RateLimitExceededError,
    SecurityConfigurationError,
    ToolTimeoutError,
    UnifiedNexusError,
)
from unified_nexus.plugins import NexusPlugin, PluginManager
from unified_nexus.security import IdempotencyCache
from unified_nexus.unified_nexus import (
    ExposureMode,
    ToolSafetyLevel,
    UnifiedNexus,
    normalize_exposure,
)

__all__ = [
    "AuthorizationError",
    "ConcurrencyLimitError",
    "DuplicateRouteError",
    "DuplicateToolError",
    "ExposureMode",
    "HumanApprovalRequiredError",
    "IdempotencyCache",
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
    "__version__",
]
