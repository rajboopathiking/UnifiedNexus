"""
UnifiedNexus: One decorator. Two interfaces.
Bridges FastAPI and Model Context Protocol (MCP).
"""

from unified_nexus.unified_nexus import (
    UnifiedNexus,
    ExposureMode,
    normalize_exposure,
)

__all__ = ["UnifiedNexus", "ExposureMode", "normalize_exposure"]
