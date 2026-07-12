"""周易插件统一运行时生命周期。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.star import StarTools

from .memory.data_migration import DataMigrationError, ensure_memory_data
from .standalone_web import StandaloneWebService

try:
    from .memory.service import MemoryService
except Exception:
    MemoryService = None  # type: ignore[assignment]
    logger.warning("长期记忆后端导入失败，Minecraft 功能继续运行", exc_info=True)


class PluginRuntime:
    """统一管理 Memory、页面、独立服务和趋势采样任务。"""

    MEMORY_TERMINATE_TIMEOUT_SECONDS = 10.0

    def __init__(self, plugin: Any, context: Any, config: Any = None) -> None:
        self.plugin = plugin
        self.context = context
        self.config = config
        self.memory: Any | None = None
        self.memory_enabled = False
        self.memory_error: str | None = None
        self.page_api: Any | None = None
        self.standalone_service: Any | None = None
        self.standalone_task: asyncio.Task[Any] | None = None
        self.standalone_error: str | None = None
        self.trend_task: asyncio.Task[Any] | None = None
        self.trend_error: str | None = None
        self.settings_changed_event = asyncio.Event()
        self._terminate_lock = asyncio.Lock()
        self._started = False
        self._terminated = False

    def start(self) -> None:
        if self._started or self._terminated:
            return
        self._started = True
        self.plugin._settings_changed_event = self.settings_changed_event
        self._start_memory()
        self._register_page_api()
        self._start_standalone()
        self._start_trend()

    def _start_standalone(self) -> None:
        try:
            self.standalone_service = StandaloneWebService()
            self.standalone_task = asyncio.create_task(self._run_standalone())
            self.standalone_error = None
        except Exception as exc:
            self.standalone_service = None
            self.standalone_task = None
            self.standalone_error = str(exc)
            logger.error("Minecraft Manager 独立页面启动任务创建失败", exc_info=True)

    def _start_trend(self) -> None:
        try:
            self.trend_task = asyncio.create_task(self.plugin._bar_data_loop())
            self.trend_error = None
        except Exception as exc:
            self.trend_task = None
            self.trend_error = str(exc)
            logger.error("Minecraft 趋势采样任务创建失败", exc_info=True)

    def _memory_config(self) -> dict[str, Any] | None:
        if self.config is None:
            return None
        try:
            value = self.config.get("memory")
        except (AttributeError, TypeError):
            return None
        return dict(value) if isinstance(value, Mapping) else None

    def _start_memory(self) -> None:
        memory_config = self._memory_config()
        if not memory_config or memory_config.get("enabled") is not True:
            return
        self.memory_enabled = True
        if MemoryService is None:
            self.memory_error = "长期记忆后端不可用"
            return
        try:
            root_data_dir = Path(StarTools.get_data_dir("astrbot_zhouyi_plugin"))
            legacy_data_dir = Path(StarTools.get_data_dir("astrbot_plugin_livingmemory"))
            migration = ensure_memory_data(legacy_data_dir, root_data_dir)
            self.memory = MemoryService(
                self.context,
                memory_config,
                str(migration.target_dir),
            )
            if migration.migrated:
                logger.info("旧长期记忆数据已迁移到根插件 memory 目录")
        except DataMigrationError as exc:
            self.memory_error = str(exc)
            logger.error("长期记忆数据迁移失败，Memory 已停用；Minecraft 功能继续运行: %s", exc)
        except Exception as exc:
            self.memory = None
            self.memory_error = str(exc)
            logger.warning("长期记忆后端启动失败，Minecraft 功能继续运行", exc_info=True)

    def _register_page_api(self) -> None:
        """优先使用统一 Facade；文件暂缺时兼容现有 MC 页面。"""
        try:
            from .zhouyi_page_api import ZhouyiDashboardApi
        except ImportError:
            try:
                from .web_api import McManagerWebApi

                self.page_api = McManagerWebApi(self.plugin)
                self.page_api.register_routes()
                logger.warning("统一页面 Facade 暂不可用，已临时注册 Minecraft Page API")
            except Exception:
                self.page_api = None
                logger.error("Minecraft Page API 注册失败", exc_info=True)
            return

        try:
            self.page_api = ZhouyiDashboardApi(self.plugin, self.memory)
            self.page_api.register_routes()
        except Exception:
            self.page_api = None
            logger.error("统一 Zhouyi Dashboard API 注册失败", exc_info=True)

    async def _run_standalone(self) -> None:
        try:
            if self.standalone_service is not None:
                await self.standalone_service.run()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("Minecraft Manager 独立页面启动失败", exc_info=True)

    def notify_settings_changed(self) -> None:
        self.settings_changed_event.set()

    @staticmethod
    def _consume_task_result(task: asyncio.Task[Any]) -> None:
        if task.cancelled():
            return
        try:
            task.exception()
        except Exception:
            pass

    async def _terminate_memory_bounded(self, memory: Any) -> None:
        task = asyncio.create_task(memory.terminate())
        _done, pending = await asyncio.wait(
            {task},
            timeout=self.MEMORY_TERMINATE_TIMEOUT_SECONDS,
        )
        if pending:
            logger.error(
                "长期记忆后端停止超时（%.1f秒），继续清理其他服务",
                self.MEMORY_TERMINATE_TIMEOUT_SECONDS,
            )
            task.cancel()
            task.add_done_callback(self._consume_task_result)
            return
        try:
            task.result()
        except asyncio.CancelledError:
            logger.warning("长期记忆后端停止任务被取消，继续清理其他服务")

    async def terminate(self) -> None:
        async with self._terminate_lock:
            if self._terminated:
                return

            memory = self.memory
            self.memory = None
            if memory is not None:
                try:
                    await self._terminate_memory_bounded(memory)
                except Exception:
                    logger.error("长期记忆后端停止失败", exc_info=True)

            if self.standalone_service is not None:
                try:
                    await self.standalone_service.stop()
                except Exception:
                    logger.error("Minecraft Manager 独立页面停止失败", exc_info=True)

            self.settings_changed_event.set()
            tasks = [task for task in (self.standalone_task, self.trend_task) if task]
            self.standalone_task = None
            self.trend_task = None
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            self._terminated = True
