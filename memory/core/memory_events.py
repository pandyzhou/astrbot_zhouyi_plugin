"""
事件处理器
负责处理AstrBot事件钩子
"""

import asyncio
import hashlib
import re
import time
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.platform import MessageType
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.core.umo_alias import parse_umo

from .base.config_manager import ConfigManager
from .base.constants import (
    FAKE_TOOL_CALL_ID_PREFIX,
    MEMORY_INJECTION_FOOTER,
    MEMORY_INJECTION_HEADER,
)
from .event_handler_modules import (
    GroupCapture,
    MemoryRecall,
    MemoryReflection,
    MessageUtils,
)
from .managers.conversation_manager import ConversationManager
from .managers.memory_engine import MemoryEngine
from .processors.memory_processor import MemoryProcessor
from .utils import (
    OperationContext,
    format_memories_for_fake_tool_call,
    format_memories_for_injection,
    get_persona_id,
)
from .utils.injection_adapter import InjectionAdapter

# 预编译记忆注入清理正则（热路径优化：避免每次调用 re.compile）
_INJECTION_CLEANUP_PATTERN = re.compile(
    re.escape(MEMORY_INJECTION_HEADER) + r".*?" + re.escape(MEMORY_INJECTION_FOOTER),
    flags=re.DOTALL,
)


class MemoryEvents:
    """事件处理器"""

    STORAGE_SHUTDOWN_TIMEOUT_SECONDS = 10.0
    GROUP_ALIAS_LOOKUP_TIMEOUT_SECONDS = 3.0
    _INVALID_GROUP_NAMES = {
        "n/a",
        "na",
        "unknown",
        "none",
        "null",
        "undefined",
        "未知",
        "未知群聊",
    }
    _GROUP_ALIAS_EVENT_MARKER = "_zhouyi_group_alias_sync_scheduled"

    def __init__(
        self,
        context: Any,
        config_manager: ConfigManager,
        memory_engine: MemoryEngine,
        memory_processor: MemoryProcessor,
        conversation_manager: ConversationManager,
    ):
        """
        初始化事件处理器

        Args:
            context: AstrBot上下文
            config_manager: 配置管理器
            memory_engine: 记忆引擎
            memory_processor: 记忆处理器
            conversation_manager: 会话管理器
        """
        self.context = context
        self.config_manager = config_manager
        self.memory_engine = memory_engine
        self.memory_processor = memory_processor
        self.conversation_manager = conversation_manager

        # 初始化子模块
        self._message_utils = MessageUtils(config_manager, conversation_manager)
        self._group_capture = GroupCapture(
            config_manager, conversation_manager, self._message_utils
        )
        self._injection_adapter = InjectionAdapter()
        self._memory_recall = MemoryRecall(
            context,
            config_manager,
            memory_engine,
            conversation_manager,
            self._message_utils,
            self._injection_adapter,
        )

        # 后台存储任务跟踪
        self._storage_tasks: set[asyncio.Task] = set()
        self._storage_sessions_inflight: set[str] = set()
        self._storage_state_lock = asyncio.Lock()
        self._shutting_down = False

        # 群聊 UMO alias 后台同步状态
        self._group_alias_tasks: dict[str, asyncio.Task] = {}
        self._group_alias_pending: dict[str, tuple[str | None, Any, str]] = {}
        self._group_alias_remote_attempted: set[str] = set()
        self._group_alias_last_known: dict[str, str] = {}

        self._memory_reflection = MemoryReflection(
            context,
            config_manager,
            memory_engine,
            memory_processor,
            conversation_manager,
            self._message_utils,
            self._storage_tasks,
            self._storage_sessions_inflight,
            self._storage_state_lock,
        )

    async def handle_all_group_messages(self, event: AstrMessageEvent):
        """Capture all group messages for memory storage"""
        self._schedule_group_alias_sync(event)
        await self._group_capture.handle_all_group_messages(event)

    async def handle_memory_recall(self, event: AstrMessageEvent, req: ProviderRequest):
        """Query and inject long-term memory before LLM request"""
        self._schedule_group_alias_sync(event)
        await self._memory_recall.handle_memory_recall(event, req)

    async def handle_memory_reflection(
        self, event: AstrMessageEvent, resp: LLMResponse
    ):
        """Check if reflection and memory storage is needed after LLM response"""
        self._schedule_group_alias_sync(event)
        await self._memory_reflection.handle_memory_reflection(event, resp)

    @classmethod
    def _normalize_group_name(
        cls, value: Any, *, group_id: str, umo: str
    ) -> str | None:
        name = str(value or "").strip()
        if (
            not name
            or name.lower() in cls._INVALID_GROUP_NAMES
            or name == group_id
            or name == umo
        ):
            return None
        return name

    @staticmethod
    def _parse_group_umo(umo: Any) -> tuple[str, str] | None:
        if not isinstance(umo, str):
            return None
        raw_parts = umo.split(":", 2)
        if len(raw_parts) != 3 or not raw_parts[0] or not raw_parts[1]:
            return None
        parsed = parse_umo(umo)
        group_id = parsed.get("session_id", "")
        if parsed.get("message_type") != "GroupMessage" or not group_id.strip():
            return None
        return umo, group_id

    def _schedule_group_alias_sync(self, event: AstrMessageEvent) -> None:
        if self._shutting_down or getattr(event, self._GROUP_ALIAS_EVENT_MARKER, False):
            return
        try:
            if event.get_message_type() != MessageType.GROUP_MESSAGE:
                return
            parsed = self._parse_group_umo(event.unified_msg_origin)
            if parsed is None:
                return
            setattr(event, self._GROUP_ALIAS_EVENT_MARKER, True)

            umo, group_id = parsed
            message_obj = getattr(event, "message_obj", None)
            group = getattr(message_obj, "group", None)
            local_name = self._normalize_group_name(
                getattr(group, "group_name", None),
                group_id=group_id,
                umo=umo,
            )
            if local_name is None and umo in self._group_alias_remote_attempted:
                return
            if local_name is not None and self._group_alias_last_known.get(umo) == local_name:
                return

            current_pending = self._group_alias_pending.get(umo)
            if local_name is not None or current_pending is None:
                self._group_alias_pending[umo] = (local_name, event, group_id)
            if umo in self._group_alias_tasks:
                return

            task = asyncio.create_task(self._run_group_alias_sync(umo))
            self._group_alias_tasks[umo] = task
            task.add_done_callback(self._consume_group_alias_task_result)
        except Exception as exc:
            logger.warning(f"群聊 UMO alias 同步调度失败: {exc}", exc_info=True)

    def _consume_group_alias_task_result(self, task: asyncio.Task) -> None:
        self._consume_storage_task_result(task)

    async def _run_group_alias_sync(self, umo: str) -> None:
        try:
            while pending := self._group_alias_pending.pop(umo, None):
                group_name, event, group_id = pending
                if group_name is None:
                    if umo in self._group_alias_remote_attempted:
                        continue
                    self._group_alias_remote_attempted.add(umo)
                    group_name = await self._lookup_group_name(event, umo, group_id)
                    if group_name is None:
                        continue
                await self._upsert_group_alias(event, umo, group_name)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(f"[{umo}] 群聊 UMO alias 同步失败: {exc}", exc_info=True)
        finally:
            self._group_alias_tasks.pop(umo, None)

    async def _lookup_onebot_group_name(
        self, event: AstrMessageEvent, umo: str, group_id: str
    ) -> str | None:
        bot = getattr(event, "bot", None)
        call_action = getattr(bot, "call_action", None)
        if not callable(call_action):
            return None

        message_obj = getattr(event, "message_obj", None)
        self_id = getattr(message_obj, "self_id", None)
        routing_params = {"self_id": self_id} if self_id else {}
        action_group_id: str | int = int(group_id) if group_id.isdigit() else group_id
        try:
            info = await asyncio.wait_for(
                call_action(
                    "get_group_info",
                    group_id=action_group_id,
                    **routing_params,
                ),
                timeout=self.GROUP_ALIAS_LOOKUP_TIMEOUT_SECONDS,
            )
            raw_name = info.get("group_name") if isinstance(info, dict) else None
            return self._normalize_group_name(raw_name, group_id=group_id, umo=umo)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug(f"[{umo}] OneBot 群资料接口未取得群名，尝试通用接口: {exc}")
            return None

    async def _lookup_group_name(
        self, event: AstrMessageEvent, umo: str, group_id: str
    ) -> str | None:
        group_name = await self._lookup_onebot_group_name(event, umo, group_id)
        if group_name is not None:
            return group_name

        get_group = getattr(event, "get_group", None)
        if not callable(get_group):
            return None
        try:
            group = await asyncio.wait_for(
                get_group(group_id),
                timeout=self.GROUP_ALIAS_LOOKUP_TIMEOUT_SECONDS,
            )
            raw_name = (
                group.get("group_name")
                if isinstance(group, dict)
                else getattr(group, "group_name", None)
            )
            return self._normalize_group_name(raw_name, group_id=group_id, umo=umo)
        except asyncio.TimeoutError:
            logger.warning(f"[{umo}] 获取群名超时，使用群号降级")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(f"[{umo}] 获取群名失败，使用群号降级: {exc}")
        return None

    async def _upsert_group_alias(
        self, event: AstrMessageEvent, umo: str, group_name: str
    ) -> None:
        if self._group_alias_last_known.get(umo) == group_name:
            return
        get_db = getattr(self.context, "get_db", None)
        if not callable(get_db):
            return

        db = get_db()
        existing = None
        get_alias = getattr(db, "get_umo_alias", None)
        if callable(get_alias):
            existing = await get_alias(umo)
        else:
            get_aliases = getattr(db, "get_umo_aliases", None)
            if callable(get_aliases):
                aliases = await get_aliases([umo])
                existing = aliases[0] if aliases else None

        existing_auto_name = str(getattr(existing, "auto_name", "") or "").strip()
        if existing_auto_name == group_name:
            self._group_alias_last_known[umo] = group_name
            return

        creator_sender_id = str(
            getattr(existing, "creator_sender_id", "")
            or (event.get_sender_id() if hasattr(event, "get_sender_id") else "")
            or ""
        )
        user_alias = getattr(existing, "user_alias", None)
        await db.upsert_umo_alias(
            umo=umo,
            creator_sender_id=creator_sender_id,
            auto_name=group_name,
            user_alias=user_alias,
        )
        self._group_alias_last_known[umo] = group_name

    async def handle_session_reset(self, event: AstrMessageEvent) -> None:
        """处理 /reset 或 /new 触发的会话清空，同步清除插件侧的消息历史和总结计数器"""
        session_id = event.unified_msg_origin
        if not session_id:
            return
        try:
            await self.conversation_manager.clear_session(session_id)
            logger.info(f"[{session_id}] 已同步清空插件会话上下文（/reset 或 /new）")
        except Exception as e:
            logger.error(f"[{session_id}] 清空插件会话上下文失败: {e}", exc_info=True)

    @staticmethod
    def _consume_storage_task_result(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        try:
            task.exception()
        except Exception:
            pass

    async def shutdown(self):
        """关闭事件处理器，等待所有存储任务完成"""
        self._shutting_down = True
        self._memory_reflection.set_shutting_down(True)

        group_alias_tasks = set(getattr(self, "_group_alias_tasks", {}).values())
        if group_alias_tasks:
            done, pending = await asyncio.wait(
                group_alias_tasks,
                timeout=self.STORAGE_SHUTDOWN_TIMEOUT_SECONDS,
            )
            if pending:
                logger.warning(f"{len(pending)} 个群聊 alias 同步任务停止超时，正在取消")
                for task in pending:
                    task.cancel()
                    task.add_done_callback(self._consume_storage_task_result)
                await asyncio.gather(*pending, return_exceptions=True)
            if done:
                await asyncio.gather(*done, return_exceptions=True)
        getattr(self, "_group_alias_tasks", {}).clear()
        getattr(self, "_group_alias_pending", {}).clear()

        if self._storage_tasks:
            tasks = set(self._storage_tasks)
            logger.info(f"等待 {len(tasks)} 个存储任务完成...")
            done, pending = await asyncio.wait(
                tasks,
                timeout=self.STORAGE_SHUTDOWN_TIMEOUT_SECONDS,
            )
            if pending:
                logger.warning(f"{len(pending)} 个存储任务停止超时，正在取消")
                for task in pending:
                    task.cancel()
                    task.add_done_callback(self._consume_storage_task_result)
                await asyncio.sleep(0)
            if done:
                await asyncio.gather(*done, return_exceptions=True)
            self._storage_tasks.clear()
        self._storage_sessions_inflight.clear()
        logger.info("MemoryEvents 已关闭")
