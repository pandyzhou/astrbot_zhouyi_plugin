from __future__ import annotations

import inspect
from functools import wraps
from typing import Any, Awaitable, Callable

from astrbot.api import logger
from astrbot.api.web import error_response, json_response

from .source_update_monitor import SourceUpdateMonitor, get_source_update_monitor
from .web_api import (
    MC_ROUTE_DESCRIPTORS,
    PAGE_API_PREFIX,
    McManagerWebApi,
    _group_sort_key,
)

PAGE_V1_PREFIX = f"{PAGE_API_PREFIX}/v1"
MC_V1_PREFIX = f"{PAGE_V1_PREFIX}/mc"
MEMORY_V1_PREFIX = f"{PAGE_V1_PREFIX}/memory"
CONFIG_V1_PREFIX = f"{PAGE_V1_PREFIX}/config"
SOURCES_V1_PREFIX = f"{PAGE_V1_PREFIX}/sources"

MemoryHandler = Callable[[], Awaitable[Any]]

# suffix, facade handler name, methods, description
MEMORY_ROUTE_DESCRIPTORS: tuple[tuple[str, str, tuple[str, ...], str], ...] = (
    ("/stats", "get_stats", ("GET",), "Zhouyi Dashboard memory stats"),
    ("/memories", "list_memories", ("GET",), "Zhouyi Dashboard memories"),
    ("/memories/detail", "get_memory_detail", ("GET",), "Zhouyi Dashboard memory detail"),
    ("/memories/update", "update_memory", ("POST",), "Zhouyi Dashboard memory update"),
    ("/memories/batch-delete", "batch_delete_memories", ("POST",), "Zhouyi Dashboard memory batch delete"),
    ("/memories/batch-update", "batch_update_memories", ("POST",), "Zhouyi Dashboard memory batch update"),
    ("/recall/test", "test_recall", ("POST",), "Zhouyi Dashboard memory recall"),
    ("/graph/overview", "get_graph_overview", ("GET",), "Zhouyi Dashboard graph overview"),
    ("/graph/query", "query_graph", ("POST",), "Zhouyi Dashboard graph query"),
    ("/backups", "list_backups", ("GET",), "Zhouyi Dashboard memory backups"),
)

MEMORY_ADMIN_ROUTE_DESCRIPTORS: tuple[tuple[str, str, tuple[str, ...], str], ...] = (
    ("/objects", "list_objects", ("GET",), "Zhouyi Dashboard memory objects"),
    ("/objects/create", "create_object", ("POST",), "Zhouyi Dashboard memory object create"),
    ("/objects/detail", "get_object_detail", ("GET",), "Zhouyi Dashboard memory object detail"),
    ("/objects/update", "update_object", ("POST",), "Zhouyi Dashboard memory object update"),
    ("/objects/revisions", "list_revisions", ("GET",), "Zhouyi Dashboard memory object revisions"),
    ("/objects/sources", "list_sources", ("GET",), "Zhouyi Dashboard memory object sources"),
    ("/objects/merge/preview", "preview_merge", ("POST",), "Zhouyi Dashboard memory merge preview"),
    ("/objects/merge", "merge_objects", ("POST",), "Zhouyi Dashboard memory object merge"),
    ("/objects/supersede", "supersede_object", ("POST",), "Zhouyi Dashboard memory object supersede"),
    ("/objects/archive", "archive_object", ("POST",), "Zhouyi Dashboard memory object archive"),
    ("/objects/batch", "batch_objects", ("POST",), "Zhouyi Dashboard memory object batch"),
    ("/conflicts", "list_conflicts", ("GET",), "Zhouyi Dashboard memory conflicts"),
    ("/conflicts/resolve", "resolve_conflict", ("POST",), "Zhouyi Dashboard memory conflict resolve"),
    ("/identities", "list_identities", ("GET",), "Zhouyi Dashboard memory identities"),
    ("/identities/owners/create", "create_owner", ("POST",), "Zhouyi Dashboard memory owner create"),
    ("/identities/owners/update", "update_owner", ("POST",), "Zhouyi Dashboard memory owner update"),
    ("/identities/aliases/link", "link_alias", ("POST",), "Zhouyi Dashboard memory alias link"),
    ("/identities/aliases/move", "move_alias", ("POST",), "Zhouyi Dashboard memory alias move"),
    ("/identities/owners/merge/preview", "preview_owner_merge", ("POST",), "Zhouyi Dashboard memory owner merge preview"),
    ("/identities/owners/merge", "merge_owners", ("POST",), "Zhouyi Dashboard memory owner merge"),
    ("/maintenance/status", "get_maintenance_status", ("GET",), "Zhouyi Dashboard memory maintenance"),
    ("/maintenance/index/retry", "retry_index", ("POST",), "Zhouyi Dashboard memory index retry"),
)


class ZhouyiDashboardApi:
    """Zhouyi Dashboard 的统一 Page API facade。"""

    def __init__(
        self,
        plugin: Any,
        memory_service: Any,
        source_update_monitor: SourceUpdateMonitor | None = None,
        *,
        memory_config_api: Any | None = None,
    ) -> None:
        self.plugin = plugin
        self.memory_service = memory_service
        self.mc_api = McManagerWebApi(plugin)
        self.source_update_monitor = source_update_monitor or get_source_update_monitor()
        self.memory_config_api = memory_config_api
        self._memory_route_handlers: dict[str, MemoryHandler] = {}
        self._memory_components: dict[str, Any] = {}
        if memory_service is not None:
            try:
                from .memory.core.page_api_modules import (
                    BackupHandler,
                    GraphHandler,
                    IdentityHandler,
                    MaintenanceHandler,
                    MemoryHandler as MemoryPageHandler,
                    MemoryObjectHandler,
                    PageApiUtils,
                    RecallHandler,
                    StatsHandler,
                )

                utils = PageApiUtils()
                legacy_memory_handler = MemoryPageHandler(utils, memory_service)
                object_handler = MemoryObjectHandler(utils, memory_service)
                identity_handler = IdentityHandler(utils, memory_service)
                maintenance_handler = MaintenanceHandler(utils, memory_service)
                self._memory_components = {
                    "get_stats": StatsHandler(utils, getattr(plugin, "context", None)),
                    "list_memories": legacy_memory_handler,
                    "get_memory_detail": legacy_memory_handler,
                    "update_memory": legacy_memory_handler,
                    "batch_delete_memories": legacy_memory_handler,
                    "batch_update_memories": legacy_memory_handler,
                    "test_recall": RecallHandler(utils),
                    "get_graph_overview": GraphHandler(utils),
                    "query_graph": GraphHandler(utils),
                    "list_backups": BackupHandler(utils, str(getattr(memory_service, "data_dir", ""))),
                    **{
                        handler_name: object_handler
                        for handler_name in (
                            "list_objects",
                            "create_object",
                            "get_object_detail",
                            "update_object",
                            "list_revisions",
                            "list_sources",
                            "preview_merge",
                            "merge_objects",
                            "supersede_object",
                            "archive_object",
                            "batch_objects",
                            "list_conflicts",
                            "resolve_conflict",
                        )
                    },
                    **{
                        handler_name: identity_handler
                        for handler_name in (
                            "list_identities",
                            "create_owner",
                            "update_owner",
                            "link_alias",
                            "move_alias",
                            "preview_owner_merge",
                            "merge_owners",
                        )
                    },
                    "get_maintenance_status": maintenance_handler,
                    "retry_index": maintenance_handler,
                }
            except Exception:
                logger.warning("Memory Page handlers 导入失败", exc_info=True)

    def register_routes(self) -> None:
        register = self.plugin.context.register_web_api

        # 新版 capability bootstrap，同时作为旧 /page/bootstrap 的兼容入口。
        register(
            f"{PAGE_V1_PREFIX}/bootstrap",
            self.bootstrap,
            ["GET"],
            "Zhouyi Dashboard bootstrap",
        )
        register(
            f"{PAGE_API_PREFIX}/bootstrap",
            self.bootstrap,
            ["GET"],
            "Zhouyi Dashboard legacy bootstrap",
        )

        for suffix, handler_name, methods, description in MC_ROUTE_DESCRIPTORS:
            handler = getattr(self.mc_api, handler_name)
            register(f"{MC_V1_PREFIX}{suffix}", handler, list(methods), description)
            if suffix != "/bootstrap":
                register(f"{PAGE_API_PREFIX}{suffix}", handler, list(methods), f"Legacy {description}")

        for suffix, handler_name, methods, description in MEMORY_ROUTE_DESCRIPTORS:
            handler = self._memory_route_handlers.setdefault(
                handler_name,
                self._make_memory_handler(handler_name),
            )
            register(f"{MEMORY_V1_PREFIX}{suffix}", handler, list(methods), description)
            register(f"{PAGE_API_PREFIX}{suffix}", handler, list(methods), f"Legacy {description}")

        for suffix, handler_name, methods, description in MEMORY_ADMIN_ROUTE_DESCRIPTORS:
            handler = self._memory_route_handlers.setdefault(
                handler_name,
                self._make_memory_handler(handler_name),
            )
            register(f"{MEMORY_V1_PREFIX}{suffix}", handler, list(methods), description)

        if self.memory_config_api is not None:
            register(
                f"{CONFIG_V1_PREFIX}/memory",
                self.memory_config_api.get_memory_config,
                ["GET"],
                "Zhouyi Dashboard memory config",
            )
            register(
                f"{CONFIG_V1_PREFIX}/memory",
                self.memory_config_api.post_memory_config,
                ["POST"],
                "Zhouyi Dashboard memory config update",
            )

        register(
            f"{SOURCES_V1_PREFIX}/updates",
            self.get_source_updates,
            ["GET"],
            "Zhouyi Dashboard source updates",
        )
        register(
            f"{SOURCES_V1_PREFIX}/updates/refresh",
            self.refresh_source_updates,
            ["POST"],
            "Zhouyi Dashboard source updates refresh",
        )

    async def get_source_updates(self):
        try:
            payload = await self.source_update_monitor.get_updates()
            return json_response({"status": "ok", "data": payload})
        except Exception as exc:
            logger.error("读取来源更新状态失败", exc_info=True)
            return error_response(
                "来源更新状态当前不可用",
                status_code=503,
                data={"code": "SOURCE_UPDATES_UNAVAILABLE", "detail": str(exc)},
            )

    async def refresh_source_updates(self):
        try:
            payload = await self.source_update_monitor.refresh()
            return json_response({"status": "ok", "data": payload})
        except Exception as exc:
            logger.error("刷新来源更新状态失败", exc_info=True)
            return error_response(
                "来源更新状态刷新失败",
                status_code=503,
                data={"code": "SOURCE_UPDATES_REFRESH_FAILED", "detail": str(exc)},
            )

    async def bootstrap(self):
        groups: list[dict[str, str]] = []
        selected_group_id: str | None = None
        mc_error: str | None = None
        try:
            group_ids = sorted(await self.mc_api._group_storages(), key=_group_sort_key)
            groups = [{"id": group_id} for group_id in group_ids]
            selected_group_id = group_ids[0] if group_ids else None
        except Exception as exc:
            logger.error("Zhouyi Dashboard MC bootstrap 失败", exc_info=True)
            mc_error = str(exc)

        memory = await self._memory_capability()
        mc_available = mc_error is None
        payload = {
            "brand": "Zhouyi Dashboard",
            "api_version": "v1",
            "groups": groups,
            "selected_group_id": selected_group_id,
            "capabilities": {
                "mc": {
                    "available": mc_available,
                    "enabled": True,
                    "initialized": mc_available,
                    "error": mc_error,
                },
                "memory": memory,
            },
        }
        return json_response({"status": "ok", "data": payload})

    async def _memory_capability(self) -> dict[str, Any]:
        service = self.memory_service
        if service is None:
            runtime = getattr(self.plugin, "runtime", None)
            enabled = bool(getattr(runtime, "memory_enabled", False))
            error = getattr(runtime, "memory_error", None) if runtime is not None else None
            return {
                "available": False,
                "enabled": enabled,
                "initialized": False,
                "error": str(error) if error else None,
                "reason": "not_enabled" if not enabled else "unavailable",
            }

        result: dict[str, Any] = {}
        for name in ("get_capability_status", "capability_status", "dashboard_status"):
            candidate = getattr(service, name, None)
            if not callable(candidate):
                continue
            try:
                value = candidate()
                if inspect.isawaitable(value):
                    value = await value
                if isinstance(value, dict):
                    result.update(value)
                break
            except Exception as exc:
                logger.error("读取 Memory capability 失败", exc_info=True)
                return {
                    "available": False,
                    "enabled": True,
                    "initialized": False,
                    "error": str(exc),
                    "reason": "status_failed",
                }

        enabled = bool(result.get("enabled", getattr(service, "enabled", True)))
        initialized_value = result.get("initialized")
        if initialized_value is None:
            initialized_value = getattr(
                service,
                "initialized",
                getattr(service, "is_initialized", getattr(service, "ready", True)),
            )
            if callable(initialized_value):
                try:
                    initialized_value = initialized_value()
                    if inspect.isawaitable(initialized_value):
                        initialized_value = await initialized_value
                except Exception as exc:
                    initialized_value = False
                    result.setdefault("error", str(exc))
        initialized = bool(initialized_value)
        error = result.get(
            "error",
            getattr(service, "initialization_error", getattr(service, "error", None)),
        )
        available = bool(result.get("available", enabled and initialized and not error))
        return {
            **result,
            "available": available,
            "enabled": enabled,
            "initialized": initialized,
            "error": str(error) if error else None,
            "reason": result.get(
                "reason",
                None if available else ("not_enabled" if not enabled else "not_initialized"),
            ),
        }

    def _make_memory_handler(self, handler_name: str) -> MemoryHandler:
        @wraps(getattr(self, "bootstrap"))
        async def handler():
            capability = await self._memory_capability()
            if not capability["available"]:
                return error_response(
                    "Memory 服务当前不可用",
                    status_code=503,
                    data={"code": "MEMORY_UNAVAILABLE", "capability": capability},
                )
            target = self._resolve_memory_handler(handler_name)
            if target is None:
                return error_response(
                    "Memory 服务未提供页面处理器",
                    status_code=503,
                    data={"code": "MEMORY_HANDLER_UNAVAILABLE"},
                )
            try:
                parameters = inspect.signature(target).parameters
                if parameters:
                    bootstrap = getattr(self.memory_service, "bootstrap", None)
                    memory_engine = getattr(bootstrap, "memory_engine", None)
                    if memory_engine is None:
                        return error_response(
                            "Memory 引擎尚未初始化",
                            status_code=503,
                            data={"code": "MEMORY_NOT_INITIALIZED"},
                        )
                    result = target(memory_engine)
                else:
                    result = target()
                if inspect.isawaitable(result):
                    result = await result
                return result
            except Exception as exc:
                logger.error(f"Memory Page API 处理失败: {handler_name}", exc_info=True)
                return error_response(
                    "Memory 服务请求失败",
                    status_code=500,
                    data={"code": "MEMORY_REQUEST_FAILED", "detail": str(exc)},
                )

        handler.__name__ = handler_name
        return handler

    def _resolve_memory_handler(self, handler_name: str) -> Callable[..., Any] | None:
        service = self.memory_service
        candidates = (
            service,
            getattr(service, "page_api", None),
            getattr(service, "handlers", None),
            getattr(service, "dashboard_api", None),
            self._memory_components.get(handler_name),
        )
        for candidate in candidates:
            if candidate is None:
                continue
            handler = getattr(candidate, handler_name, None)
            if callable(handler):
                return handler
        return None
