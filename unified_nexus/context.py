"""
NexusContext: Trusted request and execution context for UnifiedNexus.
Carries verified caller identity, permissions, correlation ID, and transport metadata.
"""

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Set, Union


@dataclass
class NexusContext:
    """
    Context passed to plugins and execution hooks during tool/route invocations.

    Attributes:
        transport:   Transport protocol ('rest' or 'mcp').
        tool_name:   Identifier of the executing tool or route.
        request_id:  Unique tracing and correlation ID.
        principal:   Verified caller identity (e.g. user ID, email, service account).
        permissions: Trusted authorization claims, roles, or scopes.
        metadata:    Additional application context (tenant ID, session ID, headers).
        start_time:  Execution start timestamp (time.perf_counter).
        duration_ms: Computed execution duration in milliseconds upon completion.
    """
    transport: Literal["rest", "mcp"] = "mcp"
    tool_name: str = "unknown"
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    principal: Optional[Union[str, Dict[str, Any]]] = None
    permissions: Set[str] = field(default_factory=set)
    metadata: Dict[str, Any] = field(default_factory=dict)
    start_time: float = field(default_factory=time.perf_counter)
    duration_ms: Optional[float] = None
    idempotency_key: Optional[str] = None

    def mark_completed(self) -> float:
        """Calculates and records execution duration in milliseconds."""
        self.duration_ms = (time.perf_counter() - self.start_time) * 1000
        return self.duration_ms

    def has_permission(self, perm: str) -> bool:
        """Checks if a specific permission claim is present."""
        return perm in self.permissions or "admin" in self.permissions or "*" in self.permissions

    @property
    def durable_idempotency_key(self) -> Optional[str]:
        """
        Returns a qualified durable idempotency key formatted as
        '{caller_id}:{tool_name}:{idempotency_key}' for use in downstream business-level
        idempotency mechanisms (e.g. database transactions, payment gateway headers).
        """
        if not self.idempotency_key:
            return None
        from unified_nexus.security import compute_caller_identity
        caller = compute_caller_identity(self.principal)
        return f"{caller}:{self.tool_name}:{self.idempotency_key}"
