"""Memory 组合组件。

该模块只承载运行时编排和业务代理，不注册 AstrBot 插件、命令或事件装饰器。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Coroutine
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult
from astrbot.api.provider import LLMResponse, ProviderRequest

from .core.base.config_manager import ConfigManager
from .core.memory_commands import MemoryCommands
from .core.memory_events import MemoryEvents
from .core.i18n_backend import init as i18n_init
from .core.i18n_backend import t
from .core.managers.backup_manager import BackupManager
from .core.memory_capture import get_active_service, set_active_service
from .core.memory_capture import is_plugin_enabled_for_session
from .core.memory_capture import is_session_enabled
from .core.memory_bootstrap import MemoryBootstrap
from .core.tools import MemoryMemorizeTool, MemorySearchTool


class MemoryService:
    """可由宿主插件组合使用的 Memory 运行时组件。"""

    VERSION = "2.3.6"
    BACKGROUND_STOP_TIMEOUT_SECONDS = 10.0
    RESOURCE_STOP_TIMEOUT_SECONDS = 10.0

    def __init__(
        self,
        context: Any,
        config: dict[str, Any] | None,
        data_dir: str,
    ) -> None:
        self.context = context
        self.data_dir = str(data_dir)
        self.config_manager: ConfigManager | None = None
        self._backup_manager: BackupManager | None = None
        self.bootstrap: MemoryBootstrap | None = None
        self.events: MemoryEvents | None = None
        self.commands: MemoryCommands | None = None
        self.evolving_memory_manager: Any | None = None
        self.evolving_memory_store: Any | None = None

        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._component_init_lock = asyncio.Lock()
        self._terminate_lock = asyncio.Lock()
        self._llm_tools_registered = False
        self._terminating = False
        self._terminated = False
        self._initialization_error: str | None = None
        self._agent_tools_error: str | None = None
        self._initialization_task: asyncio.Task[Any] | None = None

        try:
            self.config_manager = ConfigManager(config or {})
            i18n_init((config or {}).get("bot_language", "zh"))
            self._backup_manager = BackupManager(self.data_dir)
            self.bootstrap = MemoryBootstrap(
                self.context,
                self.config_manager,
                self.data_dir,
            )
            self.bootstrap.set_initialized_callback(self._on_bootstrap_initialized)
            set_active_service(self)
            self.start()
        except Exception as exc:
            self._record_initialization_failure("组件构造失败", exc)

    @property
    def ready(self) -> bool:
        """运行期事件和命令组件是否已就绪。"""
        return bool(
            not self._terminating
            and not self._terminated
            and self.bootstrap is not None
            and self.bootstrap.is_initialized
            and self.events is not None
            and self.commands is not None
        )

    @property
    def initialization_error(self) -> str | None:
        """返回组件或核心初始化阶段记录的错误。"""
        if self._initialization_error:
            return self._initialization_error
        if self.bootstrap and self.bootstrap.is_failed:
            return self.bootstrap.error_message
        return None

    @property
    def initialization_status(self) -> str:
        """返回用户可读的初始化状态。"""
        return self._get_initialization_status_message()

    @property
    def optional_errors(self) -> dict[str, str]:
        """返回不阻断核心记忆能力的可选功能错误。"""
        return (
            {"agent_tools": self._agent_tools_error}
            if self._agent_tools_error
            else {}
        )

    def start(self) -> bool:
        """非阻塞启动初始化；重复调用不会创建重复任务。"""
        if self._terminating or self._terminated or self.bootstrap is None:
            return False
        if self._initialization_task and not self._initialization_task.done():
            return True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            self._initialization_error = "Memory 初始化未启动：当前没有运行中的事件循环"
            logger.error(f"{self._initialization_error}: {exc}")
            return False

        self._initialization_error = None
        self._initialization_task = self._create_tracked_task(
            self._initialize_component(),
            loop=loop,
        )
        return True

    def _record_initialization_failure(self, stage: str, exc: BaseException) -> None:
        self._initialization_error = f"{stage}: {exc}"
        logger.error(f"Memory {self._initialization_error}", exc_info=True)

    def _create_tracked_task(
        self,
        coro: Coroutine[Any, Any, Any],
        *,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> asyncio.Task[Any]:
        """创建并跟踪后台任务。"""
        task = (loop or asyncio.get_running_loop()).create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._on_background_task_done)
        return task

    def _on_background_task_done(self, task: asyncio.Task[Any]) -> None:
        self._background_tasks.discard(task)
        if task is self._initialization_task:
            self._initialization_task = None
        if task.cancelled():
            return
        try:
            exc = task.exception()
        except Exception as callback_exc:
            logger.error(f"Memory 后台任务状态读取失败: {callback_exc}")
            return
        if exc is not None:
            self._record_initialization_failure("后台任务异常退出", exc)

    async def _on_bootstrap_initialized(self) -> None:
        """Provider 后台恢复完成后立即补齐所有运行期组件。"""
        await self._ensure_runtime_components()

    async def _initialize_component(self) -> None:
        """执行备份和核心初始化，所有失败均在组件内部记录。"""
        try:
            if self._backup_manager is None or self.bootstrap is None:
                raise RuntimeError("初始化依赖未创建")
            await self._backup_manager.backup_if_needed_async()
            success = await self.bootstrap.initialize()
            if success:
                await self._ensure_runtime_components()
            elif self.bootstrap.is_failed:
                self._initialization_error = (
                    self.bootstrap.error_message or "核心初始化失败"
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._record_initialization_failure("初始化失败", exc)

    async def _ensure_runtime_components(self) -> bool:
        """幂等创建事件处理器、命令处理器和 Agent 工具。"""
        if self._terminating or self._terminated or self.bootstrap is None:
            return False
        if not self.bootstrap.is_initialized or self.config_manager is None:
            return False

        try:
            async with self._component_init_lock:
                if self._terminating or self._terminated:
                    return False
                if not all(
                    (
                        self.bootstrap.memory_engine,
                        self.bootstrap.memory_processor,
                        self.bootstrap.conversation_manager,
                    )
                ):
                    raise RuntimeError("部分核心组件未能初始化")

                self.evolving_memory_manager = getattr(
                    self.bootstrap, "evolving_memory_manager", None
                )
                self.evolving_memory_store = getattr(
                    self.bootstrap, "evolving_memory_store", None
                )

                if self.events is None:
                    self.events = MemoryEvents(
                        context=self.context,
                        config_manager=self.config_manager,
                        memory_engine=self.bootstrap.memory_engine,
                        memory_processor=self.bootstrap.memory_processor,
                        conversation_manager=self.bootstrap.conversation_manager,
                        evolving_memory_manager=self.evolving_memory_manager,
                    )

                if self.commands is None:
                    self.commands = MemoryCommands(
                        context=self.context,
                        config_manager=self.config_manager,
                        memory_engine=self.bootstrap.memory_engine,
                        conversation_manager=self.bootstrap.conversation_manager,
                        index_validator=self.bootstrap.index_validator,
                        memory_processor=self.bootstrap.memory_processor,
                        initialization_status_callback=(
                            self._get_initialization_status_message
                        ),
                    )

                self._register_agent_tools_if_needed()
            return True
        except Exception as exc:
            self._record_initialization_failure("运行组件创建失败", exc)
            return False

    def _register_agent_tools_if_needed(self) -> None:
        """在核心组件就绪后幂等注册回忆和写入工具。"""
        if self._llm_tools_registered or self.bootstrap is None:
            return
        if self.config_manager is None:
            return
        if not self.bootstrap.memory_engine or not self.bootstrap.memory_processor:
            return

        tools: list[Any] = []
        if self.config_manager.get("agent_tools.enable_recall_tool", True):
            tools.append(
                MemorySearchTool(
                    context=self.context,
                    config_manager=self.config_manager,
                    memory_engine=self.bootstrap.memory_engine,
                    evolving_memory_manager=self.evolving_memory_manager,
                )
            )
        if self.config_manager.get("agent_tools.enable_memorize_tool", False):
            tools.append(
                MemoryMemorizeTool(
                    context=self.context,
                    memory_engine=self.bootstrap.memory_engine,
                    memory_processor=self.bootstrap.memory_processor,
                    evolving_memory_manager=self.evolving_memory_manager,
                )
            )

        try:
            if tools:
                self.context.add_llm_tools(*tools)
            self._llm_tools_registered = True
            self._agent_tools_error = None
        except Exception as exc:
            self._agent_tools_error = str(exc)
            logger.warning(f"Memory Agent 工具注册失败: {exc}", exc_info=True)

    def _schedule_passive_group_capture(self, event: AstrMessageEvent) -> None:
        """由 CustomFilter 调度群消息捕获，不唤醒消息处理管线。"""
        if (
            self._terminating
            or self._terminated
            or self.bootstrap is None
            or not self.bootstrap.is_initialized
        ):
            return
        try:
            self._create_tracked_task(self._run_passive_group_capture(event))
        except Exception as exc:
            logger.error(f"Memory 被动群捕获任务调度失败: {exc}", exc_info=True)

    async def _run_passive_group_capture(self, event: AstrMessageEvent) -> None:
        try:
            if not await is_session_enabled(event.unified_msg_origin):
                return
            if not await is_plugin_enabled_for_session(event.unified_msg_origin):
                return
            if not await self._ensure_runtime_components() or self.events is None:
                return
            await self.events.handle_all_group_messages(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f"Memory 被动群聊消息捕获失败: {exc}", exc_info=True)

    async def _ensure_plugin_ready(self) -> tuple[bool, str]:
        """确保核心与运行期组件可用。"""
        if self._terminating or self._terminated:
            return False, t("command.core_not_ready")
        if self.bootstrap is None:
            return False, self._get_initialization_status_message()
        if not await self.bootstrap.ensure_initialized():
            return False, self._get_initialization_status_message()
        if not await self._ensure_runtime_components():
            return False, t("command.core_not_ready")
        return True, ""

    def _get_initialization_status_message(self) -> str:
        if self._initialization_error:
            return t("init.failed", error=self._initialization_error)
        if self.bootstrap is None:
            return t("init.failed", error=t("common.unknown_error"))
        if self.bootstrap.is_initialized:
            return t("init.ready")
        if self.bootstrap.is_failed:
            return t(
                "init.failed",
                error=self.bootstrap.error_message or t("common.unknown_error"),
            )
        return t(
            "init.in_progress",
            attempts=self.bootstrap._provider_check_attempts,
        )

    @staticmethod
    def _command_handler_not_ready_message() -> str:
        return t("command.not_ready")

    async def handle_memory_recall(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ) -> None:
        ready, _ = await self._ensure_plugin_ready()
        if ready and self.events is not None:
            await self.events.handle_memory_recall(event, req)

    async def handle_memory_reflection(
        self,
        event: AstrMessageEvent,
        resp: LLMResponse,
    ) -> None:
        ready, _ = await self._ensure_plugin_ready()
        if ready and self.events is not None:
            await self.events.handle_memory_reflection(event, resp)

    async def handle_after_message_sent(
        self, event: AstrMessageEvent, *args: Any
    ) -> None:
        ready, _ = await self._ensure_plugin_ready()
        if ready and self.events is not None:
            await self.events.handle_after_message_sent(event, *args)

    async def handle_session_reset(self, event: AstrMessageEvent, *_args: Any) -> None:
        if not event.get_extra("_clean_ltm_session", False):
            return
        ready, _ = await self._ensure_plugin_ready()
        if ready and self.events is not None:
            await self.events.handle_session_reset(event)

    async def _proxy_command(
        self,
        event: AstrMessageEvent,
        handler_name: str,
        *args: Any,
        **kwargs: Any,
    ) -> AsyncGenerator[MessageEventResult, None]:
        ready, message = await self._ensure_plugin_ready()
        if not ready:
            yield event.plain_result(message)
            return
        if self.commands is None:
            yield event.plain_result(self._command_handler_not_ready_message())
            return
        handler = getattr(self.commands, handler_name)
        async for result in handler(event, *args, **kwargs):
            yield result

    async def status(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[MessageEventResult, None]:
        async for result in self._proxy_command(event, "handle_status"):
            yield result

    async def search(
        self,
        event: AstrMessageEvent,
        query: str,
        k: int = 5,
    ) -> AsyncGenerator[MessageEventResult, None]:
        async for result in self._proxy_command(event, "handle_search", query, k):
            yield result

    async def forget(
        self,
        event: AstrMessageEvent,
        doc_id: int,
    ) -> AsyncGenerator[MessageEventResult, None]:
        async for result in self._proxy_command(event, "handle_forget", doc_id):
            yield result

    async def rebuild_index(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[MessageEventResult, None]:
        async for result in self._proxy_command(event, "handle_rebuild_index"):
            yield result

    async def rebuild_graph(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[MessageEventResult, None]:
        async for result in self._proxy_command(event, "handle_rebuild_graph"):
            yield result

    async def webui(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[MessageEventResult, None]:
        async for result in self._proxy_command(event, "handle_webui"):
            yield result

    async def summarize(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[MessageEventResult, None]:
        async for result in self._proxy_command(event, "handle_summarize"):
            yield result

    async def reset(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[MessageEventResult, None]:
        async for result in self._proxy_command(event, "handle_reset"):
            yield result

    async def cleanup(
        self,
        event: AstrMessageEvent,
        mode: str = "preview",
    ) -> AsyncGenerator[MessageEventResult, None]:
        async for result in self._proxy_command(
            event,
            "handle_cleanup",
            dry_run=mode.lower() != "exec",
        ):
            yield result

    async def help(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[MessageEventResult, None]:
        async for result in self._proxy_command(event, "handle_help"):
            yield result

    def get_capability_status(self) -> dict[str, Any]:
        runtime_status = self.get_runtime_status()
        return {
            "available": self.ready and self.initialization_error is None,
            "enabled": True,
            "initialized": self.ready,
            "error": self.initialization_error,
            "runtime": runtime_status,
        }

    def get_runtime_status(self) -> dict[str, Any]:
        bootstrap_status = (
            self.bootstrap.get_runtime_status()
            if self.bootstrap is not None
            and hasattr(self.bootstrap, "get_runtime_status")
            else {}
        )
        feedback_status = (
            self.events.get_runtime_status() if self.events is not None else {
                "enabled": False,
                "buffered_rounds": 0,
                "buffer_count": 0,
                "task_count": 0,
                "inflight_count": 0,
                "last_status": "unavailable",
            }
        )
        return {**bootstrap_status, "feedback": feedback_status}

    @staticmethod
    def _consume_task_result(task: asyncio.Task[Any]) -> None:
        if task.cancelled():
            return
        try:
            task.exception()
        except Exception:
            pass

    async def _run_cleanup_bounded(
        self,
        resource_name: str,
        cleanup: Any,
    ) -> None:
        task = asyncio.create_task(cleanup())
        done, pending = await asyncio.wait(
            {task},
            timeout=self.RESOURCE_STOP_TIMEOUT_SECONDS,
        )
        if pending:
            logger.error(
                f"Memory {resource_name}停止超时（{self.RESOURCE_STOP_TIMEOUT_SECONDS:.1f}秒）"
            )
            task.cancel()
            task.add_done_callback(self._consume_task_result)
            return
        try:
            task.result()
        except asyncio.CancelledError:
            logger.warning(f"Memory {resource_name}停止任务被取消，继续清理")
        except Exception as exc:
            logger.error(
                f"Memory {resource_name}停止失败: {exc}",
                exc_info=True,
            )

    async def terminate(self) -> None:
        """幂等停止后台任务和所有已创建的运行资源。"""
        async with self._terminate_lock:
            if self._terminated:
                return
            self._terminating = True
            if get_active_service() is self:
                set_active_service(None)

            tasks = list(self._background_tasks)
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                _done, pending = await asyncio.wait(
                    tasks,
                    timeout=self.BACKGROUND_STOP_TIMEOUT_SECONDS,
                )
                for task in pending:
                    logger.error("Memory 后台任务停止超时，已取消并继续清理")
                    task.cancel()
                    task.add_done_callback(self._consume_task_result)
            self._background_tasks.clear()
            self._initialization_task = None

            bootstrap = self.bootstrap
            cleanup_steps: list[tuple[str, Any]] = []
            if bootstrap is not None:
                cleanup_steps.append(
                    ("初始化后台任务", bootstrap.stop_background_tasks)
                )
            if self.events is not None:
                cleanup_steps.append(("事件处理器", self.events.shutdown))
            if bootstrap is not None:
                cleanup_steps.append(
                    ("核心运行资源", bootstrap.cleanup_runtime_resources)
                )

            try:
                for resource_name, cleanup in cleanup_steps:
                    await self._run_cleanup_bounded(resource_name, cleanup)
            finally:
                self.events = None
                self.commands = None
                self.evolving_memory_manager = None
                self.evolving_memory_store = None
                self._terminated = True
                self._terminating = False
