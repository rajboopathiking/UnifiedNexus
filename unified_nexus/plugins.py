"""
NexusPlugin Protocol and PluginManager for UnifiedNexus extensibility.
Executes lifecycle and request interceptors identically across REST and MCP transports.
"""

import inspect
import logging
from typing import Any, Dict, List, Optional

from starlette.concurrency import run_in_threadpool

from unified_nexus.context import NexusContext

logger = logging.getLogger("unified-nexus.plugins")


class NexusPlugin:
    """
    Base class and interface for UnifiedNexus plugins.
    Subclass and override any of the lifecycle or execution hooks.
    Methods can be either synchronous or asynchronous.
    """

    async def startup(self, nexus: Any) -> None:
        """Called once during application startup / lifespan initialization."""
        pass

    async def before_execute(self, context: NexusContext, args: Dict[str, Any]) -> None:
        """
        Called before a tool or route function is executed.
        May inspect or enrich context, validate arguments, or raise exceptions to abort execution.
        """
        pass

    async def after_execute(self, context: NexusContext, result: Any) -> Any:
        """
        Called after successful tool or route execution.
        May inspect, mutate, or wrap the returned result.
        """
        return result

    async def on_error(self, context: NexusContext, error: Exception) -> Optional[Any]:
        """
        Called when an exception occurs during tool execution.
        May log, record metrics, or perform cleanup.
        """
        pass

    async def shutdown(self) -> None:
        """Called once during application shutdown / lifespan termination."""
        pass


class PluginManager:
    """Manages registered NexusPlugins and dispatches lifecycle and interceptor hooks."""

    def __init__(self):
        self._plugins: List[NexusPlugin] = []

    def add_plugin(self, plugin: NexusPlugin) -> None:
        """Registers a new plugin instance."""
        if not isinstance(plugin, NexusPlugin):
            # Check duck-typing if not direct subclass
            has_hook = any(
                hasattr(plugin, hook)
                for hook in ("startup", "before_execute", "after_execute", "on_error", "shutdown")
            )
            if not has_hook:
                raise TypeError(f"Expected instance of NexusPlugin, got {type(plugin).__name__}")
        self._plugins.append(plugin)

    @property
    def plugins(self) -> List[NexusPlugin]:
        return list(self._plugins)

    async def trigger_startup(self, nexus: Any) -> None:
        """Runs startup() on all registered plugins in registration order."""
        for plugin in self._plugins:
            try:
                hook = getattr(plugin, "startup", None)
                if callable(hook):
                    if inspect.iscoroutinefunction(hook):
                        await hook(nexus)
                    else:
                        await run_in_threadpool(hook, nexus)
            except Exception as e:
                logger.error(f"Plugin '{type(plugin).__name__}' startup failed: {e}")
                raise

    async def trigger_before_execute(self, context: NexusContext, args: Dict[str, Any]) -> None:
        """Runs before_execute() on all registered plugins in registration order."""
        for plugin in self._plugins:
            hook = getattr(plugin, "before_execute", None)
            if callable(hook):
                if inspect.iscoroutinefunction(hook):
                    await hook(context, args)
                else:
                    await run_in_threadpool(hook, context, args)

    async def trigger_after_execute(self, context: NexusContext, result: Any) -> Any:
        """Runs after_execute() on all registered plugins in registration order."""
        current_res = result
        for plugin in self._plugins:
            hook = getattr(plugin, "after_execute", None)
            if callable(hook):
                if inspect.iscoroutinefunction(hook):
                    modified = await hook(context, current_res)
                else:
                    modified = await run_in_threadpool(hook, context, current_res)
                if modified is not None:
                    current_res = modified
        return current_res

    async def trigger_on_error(self, context: NexusContext, error: Exception) -> None:
        """Runs on_error() on all registered plugins in reverse registration order."""
        for plugin in reversed(self._plugins):
            try:
                hook = getattr(plugin, "on_error", None)
                if callable(hook):
                    if inspect.iscoroutinefunction(hook):
                        await hook(context, error)
                    else:
                        await run_in_threadpool(hook, context, error)
            except Exception as pe:
                logger.warning(f"Plugin '{type(plugin).__name__}' on_error handler failed: {pe}")

    async def trigger_shutdown(self) -> None:
        """Runs shutdown() on all registered plugins in reverse registration order."""
        for plugin in reversed(self._plugins):
            try:
                hook = getattr(plugin, "shutdown", None)
                if callable(hook):
                    if inspect.iscoroutinefunction(hook):
                        await hook()
                    else:
                        await run_in_threadpool(hook)
            except Exception as e:
                logger.error(f"Plugin '{type(plugin).__name__}' shutdown failed: {e}")
