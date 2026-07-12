"""LivingMemory 组合组件。

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
from .core.command_handler import CommandHandler
from .core.event_handler import EventHandler
from .core.i18n_backend import init as i18n_init
from .core.i18n_backend import t
from .core.managers.backup_manager import BackupManager
from .core.passive_group_capture import get_active_component, set_active_component
from .core.passive_group_capture import is_plugin_enabled_for_session
from .core.passive_group_capture import is_session_enabled
from .core.plugin_initializer import PluginInitializer
from .core.tools import MemoryMemorizeTool, MemorySearchTool


class LivingMemoryComponent:
    """可由宿主插件组合使用的 LivingMemory 运行时组件。"""

    VERSION = "2.3.6"

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
        self.initializer: PluginInitializer | None = None
        self.event_handler: EventHandler | None = None
        self.command_handler: CommandHandler | None = None
        self.page_api: Any | None = None

        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._component_init_lock = asyncio.Lock()
        self._terminate_lock = asyncio.Lock()
        self._llm_tools_registered = False
        self._terminating = False
        self._terminated = False
        self._initialization_error: str | None = None
        self._page_api_error: str | None = None
        self._agent_tools_error: str | None = None
        self._initialization_task: asyncio.Task[Any] | None = None

        try:
            self.config_manager = ConfigManager(config or {})
            i18n_init((config or {}).get("bot_language", "zh"))
            self._backup_manager = BackupManager(self.data_dir)
            self.initializer = PluginInitializer(
                self.context,
                self.config_manager,
                self.data_dir,
            )
            set_active_component(self)
            self._register_official_page_api_if_available()
            self.start()
        except Exception as exc:
            self._record_initialization_failure("组件构造失败", exc)

    @property
    def ready(self) -> bool:
        """运行期事件和命令组件是否已就绪。"""
        return bool(
            not self._terminating
            and not self._terminated
            and self.initializer is not None
            and self.initializer.is_initialized
            and self.event_handler is not None
            and self.command_handler is not None
        )

    @property
    def initialization_error(self) -> str | None:
        """返回组件或核心初始化阶段记录的错误。"""
        if self._initialization_error:
            return self._initialization_error
        if self.initializer and self.initializer.is_failed:
            return self.initializer.error_message
        return None

    @property
    def initialization_status(self) -> str:
        """返回用户可读的初始化状态。"""
        return self._get_initialization_status_message()

    @property
    def optional_errors(self) -> dict[str, str]:
        """返回不阻断核心记忆能力的可选功能错误。"""
        errors: dict[str, str] = {}
        if self._page_api_error:
            errors["page_api"] = self._page_api_error
        if self._agent_tools_error:
            errors["agent_tools"] = self._agent_tools_error
        return errors

    def start(self) -> bool:
        """非阻塞启动初始化；重复调用不会创建重复任务。"""
        if self._terminating or self._terminated or self.initializer is None:
            return False
        if self._initialization_task and not self._initialization_task.done():
            return True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            self._initialization_error = "LivingMemory 初始化未启动：当前没有运行中的事件循环"
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
        logger.error(f"LivingMemory {self._initialization_error}", exc_info=True)

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
            logger.error(f"LivingMemory 后台任务状态读取失败: {callback_exc}")
            return
        if exc is not None:
            self._record_initialization_failure("后台任务异常退出", exc)

    def _register_official_page_api_if_available(self) -> None:
        """按宿主能力注册官方 Page API。"""
        if not hasattr(self.context, "register_web_api"):
            return
        try:
            from .core.page_api import PluginPageApi

            self.page_api = PluginPageApi(self)
            self.page_api.register_routes()
        except Exception as exc:
            self.page_api = None
            self._page_api_error = str(exc)
            logger.warning(
                f"LivingMemory Page API 注册失败，已跳过: {exc}",
                exc_info=True,
            )

    async def _initialize_component(self) -> None:
        """执行备份和核心初始化，所有失败均在组件内部记录。"""
        try:
            if self._backup_manager is None or self.initializer is None:
                raise RuntimeError("初始化依赖未创建")
            await self._backup_manager.backup_if_needed_async()
            success = await self.initializer.initialize()
            if success:
                await self._ensure_runtime_components()
            elif self.initializer.is_failed:
                self._initialization_error = (
                    self.initializer.error_message or "核心初始化失败"
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._record_initialization_failure("初始化失败", exc)

    async def _ensure_runtime_components(self) -> bool:
        """幂等创建事件处理器、命令处理器和 Agent 工具。"""
        if self._terminating or self._terminated or self.initializer is None:
            return False
        if not self.initializer.is_initialized or self.config_manager is None:
            return False

        try:
            async with self._component_init_lock:
                if self._terminating or self._terminated:
                    return False
                if not all(
                    (
                        self.initializer.memory_engine,
                        self.initializer.memory_processor,
                        self.initializer.conversation_manager,
                    )
                ):
                    raise RuntimeError("部分核心组件未能初始化")

                if self.event_handler is None:
                    self.event_handler = EventHandler(
                        context=self.context,
                        config_manager=self.config_manager,
                        memory_engine=self.initializer.memory_engine,
                        memory_processor=self.initializer.memory_processor,
                        conversation_manager=self.initializer.conversation_manager,
                    )

                if self.command_handler is None:
                    self.command_handler = CommandHandler(
                        context=self.context,
                        config_manager=self.config_manager,
                        memory_engine=self.initializer.memory_engine,
                        conversation_manager=self.initializer.conversation_manager,
                        index_validator=self.initializer.index_validator,
                        memory_processor=self.initializer.memory_processor,
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
        if self._llm_tools_registered or self.initializer is None:
            return
        if self.config_manager is None:
            return
        if not self.initializer.memory_engine or not self.initializer.memory_processor:
            return

        tools: list[Any] = []
        if self.config_manager.get("agent_tools.enable_recall_tool", True):
            tools.append(
                MemorySearchTool(
                    context=self.context,
                    config_manager=self.config_manager,
                    memory_engine=self.initializer.memory_engine,
                )
            )
        if self.config_manager.get("agent_tools.enable_memorize_tool", False):
            tools.append(
                MemoryMemorizeTool(
                    context=self.context,
                    memory_engine=self.initializer.memory_engine,
                    memory_processor=self.initializer.memory_processor,
                )
            )

        try:
            if tools:
                self.context.add_llm_tools(*tools)
            self._llm_tools_registered = True
            self._agent_tools_error = None
        except Exception as exc:
            self._agent_tools_error = str(exc)
            logger.warning(f"LivingMemory Agent 工具注册失败: {exc}", exc_info=True)

    def _schedule_passive_group_capture(self, event: AstrMessageEvent) -> None:
        """由 CustomFilter 调度群消息捕获，不唤醒消息处理管线。"""
        if (
            self._terminating
            or self._terminated
            or self.initializer is None
            or not self.initializer.is_initialized
        ):
            return
        try:
            self._create_tracked_task(self._run_passive_group_capture(event))
        except Exception as exc:
            logger.error(f"LivingMemory 被动群捕获任务调度失败: {exc}", exc_info=True)

    async def _run_passive_group_capture(self, event: AstrMessageEvent) -> None:
        try:
            if not await is_session_enabled(event.unified_msg_origin):
                return
            if not await is_plugin_enabled_for_session(event.unified_msg_origin):
                return
            if not await self._ensure_runtime_components() or self.event_handler is None:
                return
            await self.event_handler.handle_all_group_messages(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f"LivingMemory 被动群聊消息捕获失败: {exc}", exc_info=True)

    async def _ensure_plugin_ready(self) -> tuple[bool, str]:
        """确保核心与运行期组件可用。"""
        if self._terminating or self._terminated:
            return False, t("command.core_not_ready")
        if self.initializer is None:
            return False, self._get_initialization_status_message()
        if not await self.initializer.ensure_initialized():
            return False, self._get_initialization_status_message()
        if not await self._ensure_runtime_components():
            return False, t("command.core_not_ready")
        return True, ""

    def _get_initialization_status_message(self) -> str:
        if self._initialization_error:
            return t("init.failed", error=self._initialization_error)
        if self.initializer is None:
            return t("init.failed", error=t("common.unknown_error"))
        if self.initializer.is_initialized:
            return t("init.ready")
        if self.initializer.is_failed:
            return t(
                "init.failed",
                error=self.initializer.error_message or t("common.unknown_error"),
            )
        return t(
            "init.in_progress",
            attempts=self.initializer._provider_check_attempts,
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
        if ready and self.event_handler is not None:
            await self.event_handler.handle_memory_recall(event, req)

    async def handle_memory_reflection(
        self,
        event: AstrMessageEvent,
        resp: LLMResponse,
    ) -> None:
        ready, _ = await self._ensure_plugin_ready()
        if ready and self.event_handler is not None:
            await self.event_handler.handle_memory_reflection(event, resp)

    async def handle_session_reset(self, event: AstrMessageEvent, *_args: Any) -> None:
        if not event.get_extra("_clean_ltm_session", False):
            return
        ready, _ = await self._ensure_plugin_ready()
        if ready and self.event_handler is not None:
            await self.event_handler.handle_session_reset(event)

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
        if self.command_handler is None:
            yield event.plain_result(self._command_handler_not_ready_message())
            return
        handler = getattr(self.command_handler, handler_name)
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

    async def terminate(self) -> None:
        """幂等停止后台任务和所有已创建的运行资源。"""
        async with self._terminate_lock:
            if self._terminated:
                return
            self._terminating = True
            if get_active_component() is self:
                set_active_component(None)

            tasks = list(self._background_tasks)
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            self._background_tasks.clear()
            self._initialization_task = None

            initializer = self.initializer
            cleanup_steps: list[tuple[str, Any]] = []
            if initializer is not None:
                cleanup_steps.append(
                    ("初始化后台任务", initializer.stop_background_tasks)
                )
            if self.event_handler is not None:
                cleanup_steps.append(("事件处理器", self.event_handler.shutdown))
            if initializer is not None:
                cleanup_steps.append(("衰减调度器", initializer.stop_scheduler))
                conversation_manager = initializer.conversation_manager
                if conversation_manager and conversation_manager.store:
                    cleanup_steps.append(
                        ("会话存储", conversation_manager.store.close)
                    )
                if initializer.memory_engine:
                    cleanup_steps.append(("记忆引擎", initializer.memory_engine.close))
                if initializer.db:
                    cleanup_steps.append(("向量数据库", initializer.db.close))

            try:
                for resource_name, cleanup in cleanup_steps:
                    try:
                        await cleanup()
                    except Exception as exc:
                        logger.error(
                            f"LivingMemory {resource_name}停止失败: {exc}",
                            exc_info=True,
                        )
            finally:
                self._terminated = True
                self._terminating = False
