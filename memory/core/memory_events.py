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
    RECALL_TRACE_EXTRA_KEY,
    RESPONSE_CONTEXT_EXTRA_KEY,
    OperationContext,
    build_access_context_from_event,
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
    FEEDBACK_SHUTDOWN_TIMEOUT_SECONDS = 10.0
    GROUP_ALIAS_LOOKUP_TIMEOUT_SECONDS = 3.0
    _STRONG_FEEDBACK_SIGNAL = re.compile(
        r"(?:不对|不是|错了|纠正|更正|记住|别忘|我喜欢|我不喜欢|我偏好|我的名字|我是|叫我|"
        r"他是我的|她是我的|我们是|关系是|actually|correction|remember|prefer|my name is)",
        re.IGNORECASE,
    )
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
        evolving_memory_manager: Any | None = None,
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
        self.evolving_memory_manager = evolving_memory_manager or getattr(
            memory_engine, "evolving_memory_manager", None
        )

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

        # 回复后反馈缓冲与后台任务。
        self._feedback_buffers: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._feedback_idle_tasks: dict[tuple[str, str], asyncio.Task] = {}
        self._feedback_tasks: set[asyncio.Task] = set()
        self._feedback_inflight: set[tuple[str, str]] = set()
        self._feedback_lock = asyncio.Lock()
        self._feedback_status: dict[str, Any] = {
            "last_status": "idle",
            "last_error_hash": None,
            "completed_batches": 0,
            "failed_batches": 0,
        }

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
        """记录最终可见助手回复并触发现有总结；反馈延后到发送完成。"""
        self._schedule_group_alias_sync(event)
        await self._memory_reflection.handle_memory_reflection(event, resp)

        if getattr(resp, "role", None) != "assistant":
            return
        if getattr(resp, "tools_call_name", None):
            return
        response_text = str(getattr(resp, "completion_text", "") or "").strip()
        if not response_text:
            return
        user_text = await self._message_utils.get_event_message_str(event)
        persona_id = await get_persona_id(self.context, event)
        trace = event.get_extra(RECALL_TRACE_EXTRA_KEY, [])
        event.set_extra(
            RESPONSE_CONTEXT_EXTRA_KEY,
            {
                "user_text": user_text,
                "assistant_text": response_text,
                "persona_id": persona_id,
                "recall_trace": list(trace) if isinstance(trace, list) else [],
                "captured_at": time.time(),
            },
        )

    async def handle_after_message_sent(
        self, event: AstrMessageEvent, *_args: Any
    ) -> None:
        """发送成功后处理反馈；reset 事件先丢弃旧缓冲再清理会话。"""
        if event.get_extra("_clean_ltm_session", False):
            await self._discard_feedback_buffer_for_event(event)
            await self.handle_session_reset(event)
            return
        await self.handle_memory_feedback(event)

    async def _discard_feedback_buffer_for_event(self, event: AstrMessageEvent) -> None:
        session_id = str(getattr(event, "unified_msg_origin", "") or "").strip()
        if not session_id:
            return
        keys = [
            key
            for key in getattr(self, "_feedback_buffers", {})
            if key[1] == session_id
        ]
        async with self._feedback_lock:
            for key in keys:
                self._feedback_buffers.pop(key, None)
                idle_task = self._feedback_idle_tasks.pop(key, None)
                if idle_task is not None and not idle_task.done():
                    idle_task.cancel()

    async def handle_memory_feedback(self, event: AstrMessageEvent) -> None:
        """按 owner/session 缓冲一轮已发送对话并按配置调度反馈。"""
        manager = self.evolving_memory_manager
        if self._shutting_down or manager is None or self.memory_processor is None:
            return
        config = getattr(manager, "evolving_config", {}) or {}
        if not config.get("enabled", True) or not config.get("feedback_enabled", True):
            return
        response_context = event.get_extra(RESPONSE_CONTEXT_EXTRA_KEY, None)
        if not isinstance(response_context, dict):
            return
        user_text = str(response_context.get("user_text") or "").strip()
        assistant_text = str(response_context.get("assistant_text") or "").strip()
        if not assistant_text:
            return
        access_context = await build_access_context_from_event(
            event,
            manager,
            astrbot_context=self.context,
            persona_id=response_context.get("persona_id"),
        )
        if access_context is None:
            return

        raw_trace = response_context.get("recall_trace")
        recall_trace = []
        if isinstance(raw_trace, list):
            for entry in raw_trace:
                if not isinstance(entry, dict):
                    continue
                trace_context = entry.get("context") or entry.get("access_context")
                if isinstance(trace_context, dict) and trace_context.get(
                    "owner_user_id"
                ) != access_context.owner_user_id:
                    continue
                if entry.get("memory_item_id"):
                    recall_trace.append(dict(entry))

        key = (access_context.owner_user_id, access_context.session_id)
        round_payload = {
            "conversation": [
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": assistant_text},
            ],
            "recall_trace": recall_trace,
            "access_context": access_context,
        }
        async with self._feedback_lock:
            buffer = self._feedback_buffers.setdefault(key, [])
            buffer.append(round_payload)
            buffered_rounds = len(buffer)
            self._schedule_feedback_idle_locked(key, config)

        trigger_mode = str(config.get("feedback_trigger_mode", "adaptive"))
        batch_rounds = max(1, int(config.get("feedback_batch_rounds", 3) or 3))
        immediate = trigger_mode == "immediate"
        if trigger_mode == "adaptive":
            immediate = bool(recall_trace) or bool(
                self._STRONG_FEEDBACK_SIGNAL.search(user_text)
            )
        if immediate or buffered_rounds >= batch_rounds:
            self._schedule_feedback_flush(key)

    def _schedule_feedback_idle_locked(
        self, key: tuple[str, str], config: dict[str, Any]
    ) -> None:
        previous = self._feedback_idle_tasks.pop(key, None)
        if previous is not None and not previous.done():
            previous.cancel()
        idle_seconds = max(0.01, float(config.get("feedback_idle_seconds", 300) or 300))
        task = asyncio.create_task(self._idle_feedback_flush(key, idle_seconds))
        self._feedback_idle_tasks[key] = task
        self._feedback_tasks.add(task)
        task.add_done_callback(self._on_feedback_task_done)

    async def _idle_feedback_flush(
        self, key: tuple[str, str], idle_seconds: float
    ) -> None:
        try:
            await asyncio.sleep(idle_seconds)
            self._schedule_feedback_flush(key)
        finally:
            current = self._feedback_idle_tasks.get(key)
            if current is asyncio.current_task():
                self._feedback_idle_tasks.pop(key, None)

    def _schedule_feedback_flush(
        self, key: tuple[str, str], *, allow_shutdown: bool = False
    ) -> None:
        if (self._shutting_down and not allow_shutdown) or key in self._feedback_inflight:
            return
        idle_task = self._feedback_idle_tasks.pop(key, None)
        if idle_task is not None and not idle_task.done():
            idle_task.cancel()
        self._feedback_inflight.add(key)
        try:
            task = asyncio.create_task(self._flush_feedback_buffer(key))
        except Exception:
            self._feedback_inflight.discard(key)
            raise
        self._feedback_tasks.add(task)
        task.add_done_callback(self._on_feedback_task_done)

    async def _flush_feedback_buffer(self, key: tuple[str, str]) -> None:
        batch: list[dict[str, Any]] = []
        try:
            async with self._feedback_lock:
                batch = self._feedback_buffers.pop(key, [])
            if not batch:
                return
            conversation: list[dict[str, Any]] = []
            recall_trace: list[dict[str, Any]] = []
            trace_seen: set[tuple[str, int]] = set()
            for round_payload in batch:
                conversation.extend(round_payload["conversation"])
                for entry in round_payload["recall_trace"]:
                    trace_key = (
                        str(entry.get("memory_item_id") or ""),
                        int(entry.get("version", 0) or 0),
                    )
                    if trace_key[0] and trace_key not in trace_seen:
                        recall_trace.append(entry)
                        trace_seen.add(trace_key)
            access_context = batch[-1]["access_context"]
            result = await self.memory_processor.evaluate_memory_feedback(
                conversation=conversation,
                recall_trace=recall_trace,
                access_context=access_context,
                evolving_manager=self.evolving_memory_manager,
            )
            self._feedback_status["last_status"] = str(
                (result or {}).get("status", "completed")
            )
            self._feedback_status["completed_batches"] += 1
        except asyncio.CancelledError:
            if batch:
                await self._restore_feedback_batch(key, batch)
            raise
        except Exception as exc:
            if batch:
                await self._restore_feedback_batch(key, batch)
            digest = hashlib.sha256(type(exc).__name__.encode("utf-8")).hexdigest()[:16]
            self._feedback_status["last_status"] = "failed"
            self._feedback_status["last_error_hash"] = digest
            self._feedback_status["failed_batches"] += 1
            logger.warning(
                "记忆反馈批次失败 owner_hash=%s session_hash=%s error_hash=%s",
                hashlib.sha256(key[0].encode("utf-8")).hexdigest()[:12],
                hashlib.sha256(key[1].encode("utf-8")).hexdigest()[:12],
                digest,
                exc_info=True,
            )
        finally:
            self._feedback_inflight.discard(key)

    async def _restore_feedback_batch(
        self, key: tuple[str, str], batch: list[dict[str, Any]]
    ) -> None:
        async with self._feedback_lock:
            current = self._feedback_buffers.get(key, [])
            self._feedback_buffers[key] = [*batch, *current]
            if not self._shutting_down:
                config = getattr(
                    self.evolving_memory_manager, "evolving_config", {}
                ) or {}
                self._schedule_feedback_idle_locked(key, config)

    def _on_feedback_task_done(self, task: asyncio.Task) -> None:
        self._feedback_tasks.discard(task)
        if task.cancelled():
            return
        try:
            task.exception()
        except Exception:
            pass

    def get_runtime_status(self) -> dict[str, Any]:
        manager = self.evolving_memory_manager
        config = getattr(manager, "evolving_config", {}) if manager is not None else {}
        return {
            "enabled": bool(
                manager is not None
                and config.get("enabled", True)
                and config.get("feedback_enabled", True)
            ),
            "buffered_rounds": sum(len(value) for value in self._feedback_buffers.values()),
            "buffer_count": len(self._feedback_buffers),
            "task_count": len(self._feedback_tasks),
            "inflight_count": len(self._feedback_inflight),
            **self._feedback_status,
        }

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

        feedback_idle_tasks = getattr(self, "_feedback_idle_tasks", {})
        for idle_task in list(feedback_idle_tasks.values()):
            if not idle_task.done():
                idle_task.cancel()
        feedback_idle_tasks.clear()
        for key in list(getattr(self, "_feedback_buffers", {})):
            self._schedule_feedback_flush(key, allow_shutdown=True)
        feedback_tasks = set(getattr(self, "_feedback_tasks", set()))
        if feedback_tasks:
            done, pending = await asyncio.wait(
                feedback_tasks,
                timeout=self.FEEDBACK_SHUTDOWN_TIMEOUT_SECONDS,
            )
            if pending:
                logger.warning(f"{len(pending)} 个记忆反馈任务停止超时，正在取消")
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
            if done:
                await asyncio.gather(*done, return_exceptions=True)
        getattr(self, "_feedback_tasks", set()).clear()
        getattr(self, "_feedback_inflight", set()).clear()

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
