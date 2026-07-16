"""
记忆反思模块
负责检查是否需要记忆总结和存储
"""

import asyncio
import hashlib
from typing import TYPE_CHECKING, Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.platform import MessageType
from astrbot.api.provider import LLMResponse

from ..models.evolving_memory import MemoryActorType, MemoryScope
from ..utils import get_persona_id

if TYPE_CHECKING:
    from ..base.config_manager import ConfigManager
    from ..managers.conversation_manager import ConversationManager
    from ..managers.memory_engine import MemoryEngine
    from ..processors.memory_processor import MemoryProcessor
    from .message_utils import MessageUtils


def build_summary_identity(
    event: AstrMessageEvent,
    *,
    session_id: str,
    is_group: bool,
    history_messages: list[Any] | None = None,
) -> dict[str, Any] | None:
    """Capture the exact identity tuple needed for owner-scoped summary writes."""

    def event_value(method_name: str) -> str:
        method = getattr(event, method_name, None)
        if not callable(method):
            return ""
        try:
            return str(method() or "").strip()
        except Exception:
            return ""

    platform_id = event_value("get_platform_name")
    if not platform_id:
        platform_id = session_id.split(":", 1)[0].strip()

    bot_id = event_value("get_self_id")
    if not bot_id:
        bot_id = str(
            getattr(getattr(event, "message_obj", None), "self_id", "") or ""
        ).strip()
    external_user_id = event_value("get_sender_id")
    if not external_user_id:
        sender = getattr(getattr(event, "message_obj", None), "sender", None)
        external_user_id = str(getattr(sender, "user_id", "") or "").strip()

    if not platform_id or not bot_id or not external_user_id:
        return None
    return {
        "platform_id": platform_id,
        "bot_id": bot_id,
        "external_user_id": external_user_id,
        "session_id": session_id,
        "is_group": bool(is_group),
    }


def get_summary_message_id_range(history_messages: list[Any]) -> tuple[int | None, int | None]:
    message_ids = []
    for message in history_messages:
        try:
            message_id = int(getattr(message, "id", 0) or 0)
        except (TypeError, ValueError):
            continue
        if message_id > 0:
            message_ids.append(message_id)
    if not message_ids:
        return None, None
    return min(message_ids), max(message_ids)


async def _persist_summary_key_facts_once(
    *,
    memory_engine: "MemoryEngine",
    identity: dict[str, Any] | None,
    history_messages: list[Any],
    persona_id: str | None,
    metadata: dict[str, Any],
    legacy_document_id: int,
    importance: float,
    triggered_by: str,
) -> dict[str, Any]:
    """Execute one dual-write attempt without mutating the retry queue."""
    raw_facts = metadata.get("key_facts")
    facts = []
    if isinstance(raw_facts, list):
        facts = list(
            dict.fromkeys(str(value).strip() for value in raw_facts if str(value).strip())
        )
    stats: dict[str, Any] = {
        "attempted": len(facts),
        "created": 0,
        "deduplicated": 0,
        "failed": 0,
        "skipped": 0,
        "errors": [],
    }
    if not facts:
        return stats

    manager = getattr(memory_engine, "evolving_memory_manager", None)
    if manager is None or not manager.evolving_config.get("enabled", True):
        stats["skipped"] = len(facts)
        return stats
    if not manager.evolving_config.get("write_enabled", True):
        stats["skipped"] = len(facts)
        return stats
    if identity is None:
        stats["skipped"] = len(facts)
        stats["errors"].append("无法解析 owner identity")
        return stats

    message_start_id, message_end_id = get_summary_message_id_range(history_messages)
    if message_start_id is None or message_end_id is None:
        stats["skipped"] = len(facts)
        stats["errors"].append("总结消息缺少可追溯的数据库 ID")
        return stats

    try:
        access_context = await manager.build_access_context(
            platform_id=str(identity["platform_id"]),
            bot_id=str(identity["bot_id"]),
            external_user_id=str(identity["external_user_id"]),
            session_id=str(identity["session_id"]),
            persona_id=persona_id,
            is_group=bool(identity["is_group"]),
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        stats["failed"] = len(facts)
        stats["errors"].append(str(exc))
        logger.warning("总结 key_facts 无法建立 owner 上下文", exc_info=True)
        return stats

    canonical_summary = str(
        metadata.get("canonical_summary") or metadata.get("persona_summary") or ""
    ).strip()
    target_scope = (
        MemoryScope.SESSION
        if access_context.is_group
        else MemoryScope.PERSONA
        if access_context.persona_id
        else MemoryScope.USER
    )
    for fact_index, fact in enumerate(facts):
        digest_input = "\x1f".join(
            (
                access_context.owner_user_id,
                access_context.session_id,
                str(message_start_id),
                str(message_end_id),
                fact.casefold(),
            )
        )
        digest = hashlib.sha256(digest_input.encode("utf-8")).hexdigest()
        operation_key = f"summary-feedback:{digest}"
        source = {
            "source_key": f"summary-key-fact:{digest}",
            "source_type": "summary_key_fact",
            "source_ref": f"document:{legacy_document_id}",
            "document_id": int(legacy_document_id),
            "session_id": access_context.session_id,
            "message_start_id": message_start_id,
            "message_end_id": message_end_id,
            "content_snapshot": (
                fact
                if not canonical_summary
                else f"{fact}\n\n{canonical_summary[:4096]}"
            ),
            "metadata": {
                "fact_index": fact_index,
                "triggered_by": triggered_by,
                "legacy_document_id": int(legacy_document_id),
            },
        }
        try:
            result = await manager.create(
                context=access_context,
                content=fact,
                operation_key=operation_key,
                scope=target_scope,
                importance=max(0.0, min(1.0, float(importance))),
                confidence=max(
                    0.0,
                    min(1.0, float(metadata.get("confidence", 0.7))),
                ),
                actor_type=MemoryActorType.AUTOMATIC,
                actor_id=triggered_by,
                reason="summary key_facts dual-write",
                source=source,
            )
            if result.deduplicated:
                stats["deduplicated"] += 1
            else:
                stats["created"] += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            stats["failed"] += 1
            fact_hash = hashlib.sha256(fact.encode("utf-8")).hexdigest()[:12]
            stats["errors"].append(
                f"fact#{fact_index}:{type(exc).__name__}:{fact_hash}"
            )
            logger.warning(
                "总结 key_fact 对象写入失败: fact_index=%d fact_hash=%s error=%s",
                fact_index,
                fact_hash,
                type(exc).__name__,
                exc_info=True,
            )
    return stats


def _get_summary_key_facts_retry_queue(
    memory_engine: "MemoryEngine",
) -> dict[str, dict[str, Any]]:
    queue = getattr(memory_engine, "_summary_key_facts_retry_queue", None)
    if not isinstance(queue, dict):
        queue = {}
        setattr(memory_engine, "_summary_key_facts_retry_queue", queue)
    return queue


def _get_summary_key_facts_retry_lock(memory_engine: "MemoryEngine") -> asyncio.Lock:
    lock = getattr(memory_engine, "_summary_key_facts_retry_lock", None)
    if not isinstance(lock, asyncio.Lock):
        lock = asyncio.Lock()
        setattr(memory_engine, "_summary_key_facts_retry_lock", lock)
    return lock


def _build_summary_key_facts_retry_key(
    *,
    identity: dict[str, Any] | None,
    history_messages: list[Any],
    metadata: dict[str, Any],
    legacy_document_id: int,
) -> str:
    message_start_id, message_end_id = get_summary_message_id_range(history_messages)
    facts = metadata.get("key_facts")
    normalized_facts = (
        [str(value).strip().casefold() for value in facts]
        if isinstance(facts, list)
        else []
    )
    identity_parts = []
    if identity is not None:
        identity_parts = [
            str(identity.get(key) or "")
            for key in (
                "platform_id",
                "bot_id",
                "external_user_id",
                "session_id",
                "is_group",
            )
        ]
    digest_input = "\x1f".join(
        [
            str(legacy_document_id),
            str(message_start_id or ""),
            str(message_end_id or ""),
            *identity_parts,
            *normalized_facts,
        ]
    )
    return hashlib.sha256(digest_input.encode("utf-8")).hexdigest()


async def _enqueue_summary_key_facts_retry(
    *,
    memory_engine: "MemoryEngine",
    identity: dict[str, Any] | None,
    history_messages: list[Any],
    persona_id: str | None,
    metadata: dict[str, Any],
    legacy_document_id: int,
    importance: float,
    triggered_by: str,
) -> None:
    retry_key = _build_summary_key_facts_retry_key(
        identity=identity,
        history_messages=history_messages,
        metadata=metadata,
        legacy_document_id=legacy_document_id,
    )
    payload_metadata = dict(metadata)
    if isinstance(metadata.get("key_facts"), list):
        payload_metadata["key_facts"] = list(metadata["key_facts"])
    payload = {
        "identity": dict(identity) if identity is not None else None,
        "history_messages": list(history_messages),
        "persona_id": persona_id,
        "metadata": payload_metadata,
        "legacy_document_id": int(legacy_document_id),
        "importance": float(importance),
        "triggered_by": triggered_by,
        "retry_count": 0,
    }
    lock = _get_summary_key_facts_retry_lock(memory_engine)
    async with lock:
        queue = _get_summary_key_facts_retry_queue(memory_engine)
        queue.setdefault(retry_key, payload)


def _summary_key_facts_attempt_complete(stats: dict[str, Any]) -> bool:
    attempted = int(stats.get("attempted", 0) or 0)
    persisted = int(stats.get("created", 0) or 0) + int(
        stats.get("deduplicated", 0) or 0
    )
    return int(stats.get("failed", 0) or 0) == 0 and persisted >= attempted


async def retry_pending_summary_key_facts(
    memory_engine: "MemoryEngine | None",
) -> dict[str, int]:
    """Retry queued object dual-writes without creating another legacy document."""
    totals = {
        "attempted": 0,
        "completed": 0,
        "created": 0,
        "deduplicated": 0,
        "failed": 0,
        "remaining": 0,
    }
    if memory_engine is None:
        return totals
    queue = getattr(memory_engine, "_summary_key_facts_retry_queue", None)
    if not isinstance(queue, dict) or not queue:
        return totals

    lock = _get_summary_key_facts_retry_lock(memory_engine)
    async with lock:
        queue = _get_summary_key_facts_retry_queue(memory_engine)
        for retry_key, payload in list(queue.items()):
            totals["attempted"] += 1
            try:
                stats = await _persist_summary_key_facts_once(
                    memory_engine=memory_engine,
                    identity=payload["identity"],
                    history_messages=payload["history_messages"],
                    persona_id=payload["persona_id"],
                    metadata=payload["metadata"],
                    legacy_document_id=payload["legacy_document_id"],
                    importance=payload["importance"],
                    triggered_by=payload["triggered_by"],
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                payload["retry_count"] = int(payload.get("retry_count", 0)) + 1
                totals["failed"] += 1
                logger.warning(
                    "重试总结 key_facts 双写异常: retry_key=%s error=%s",
                    retry_key[:12],
                    type(exc).__name__,
                    exc_info=True,
                )
                continue

            totals["created"] += int(stats.get("created", 0) or 0)
            totals["deduplicated"] += int(stats.get("deduplicated", 0) or 0)
            if _summary_key_facts_attempt_complete(stats):
                queue.pop(retry_key, None)
                totals["completed"] += 1
            else:
                payload["retry_count"] = int(payload.get("retry_count", 0)) + 1
                totals["failed"] += 1
        totals["remaining"] = len(queue)

    if totals["attempted"]:
        logger.info(
            "总结 key_facts 待重试队列处理完成: attempted=%d completed=%d "
            "created=%d deduplicated=%d remaining=%d",
            totals["attempted"],
            totals["completed"],
            totals["created"],
            totals["deduplicated"],
            totals["remaining"],
        )
    return totals


async def persist_summary_key_facts(
    *,
    memory_engine: "MemoryEngine",
    identity: dict[str, Any] | None,
    history_messages: list[Any],
    persona_id: str | None,
    metadata: dict[str, Any],
    legacy_document_id: int,
    importance: float,
    triggered_by: str,
) -> dict[str, Any]:
    """Best-effort dual-write with an in-process idempotent retry queue."""
    stats = await _persist_summary_key_facts_once(
        memory_engine=memory_engine,
        identity=identity,
        history_messages=history_messages,
        persona_id=persona_id,
        metadata=metadata,
        legacy_document_id=legacy_document_id,
        importance=importance,
        triggered_by=triggered_by,
    )
    stats["queued"] = 0
    if int(stats.get("failed", 0) or 0) > 0:
        await _enqueue_summary_key_facts_retry(
            memory_engine=memory_engine,
            identity=identity,
            history_messages=history_messages,
            persona_id=persona_id,
            metadata=metadata,
            legacy_document_id=legacy_document_id,
            importance=importance,
            triggered_by=triggered_by,
        )
        stats["queued"] = 1
    return stats


class MemoryReflection:
    """记忆反思类"""

    def __init__(
        self,
        context: Any,
        config_manager: "ConfigManager",
        memory_engine: "MemoryEngine",
        memory_processor: "MemoryProcessor",
        conversation_manager: "ConversationManager",
        message_utils: "MessageUtils",
        storage_tasks: set[asyncio.Task],
        storage_sessions_inflight: set[str],
        storage_state_lock: asyncio.Lock,
    ):
        """
        初始化记忆反思模块

        Args:
            context: AstrBot上下文
            config_manager: 配置管理器
            memory_engine: 记忆引擎
            memory_processor: 记忆处理器
            conversation_manager: 会话管理器
            message_utils: 消息处理工具
            storage_tasks: 后台存储任务集合（共享状态）
            storage_sessions_inflight: 正在处理的会话集合（共享状态）
            storage_state_lock: 存储状态锁（共享状态）
        """
        self.context = context
        self.config_manager = config_manager
        self.memory_engine = memory_engine
        self.memory_processor = memory_processor
        self.conversation_manager = conversation_manager
        self.message_utils = message_utils
        self._storage_tasks = storage_tasks
        self._storage_sessions_inflight = storage_sessions_inflight
        self._storage_state_lock = storage_state_lock
        self._shutting_down = False

    async def handle_memory_reflection(
        self, event: AstrMessageEvent, resp: LLMResponse
    ):
        """Check if reflection and memory storage is needed after LLM response"""
        logger.debug(
            f"[DEBUG-Reflection] 进入 handle_memory_reflection，resp.role={resp.role}"
        )

        if resp.role != "assistant":
            return

        # 过滤 tool 循环中间轮次（有工具调用时跳过，等待最终总结轮）
        if resp.tools_call_name:
            logger.debug(
                f"[DEBUG-Reflection] 检测到工具调用响应（tools={resp.tools_call_name}），跳过记录"
            )
            return

        try:
            session_id = event.unified_msg_origin
            logger.debug(f"[DEBUG-Reflection] 获取到 unified_msg_origin: {session_id}")

            if not session_id:
                logger.warning("[DEBUG-Reflection] session_id 为空，跳过反思")
                return

            # 检测异常session_id
            if "Error:" in session_id or "error:" in session_id.lower():
                logger.warning(
                    f"[{session_id}] 检测到异常的session_id，这可能导致记忆总结异常。"
                )

            # 每个最终响应循环先重放历史双写失败；该路径只写对象层，
            # 不会重新生成或重复存储 legacy 总结文档。
            await retry_pending_summary_key_facts(self.memory_engine)

            # 检查响应内容是否有效（过滤空回复和错误）
            response_text = resp.completion_text
            if not response_text or not response_text.strip():
                logger.debug(f"[{session_id}] 模型返回空回复，跳过记录")
                return

            # 检查是否为错误响应
            error_indicators = [
                "api error",
                "request failed",
                "rate limit",
                "timeout",
                "connection error",
                "服务暂时不可用",
                "请求失败",
                "接口错误",
            ]
            response_lower = response_text.lower()
            if any(indicator in response_lower for indicator in error_indicators):
                logger.debug(
                    f"[{session_id}] 检测到错误响应，跳过记录: {response_text[:50]}..."
                )
                return

            # 添加助手响应
            await self.conversation_manager.add_message_from_event(
                event=event,
                role="assistant",
                content=response_text,
            )
            logger.debug(f"[DEBUG-Reflection] [{session_id}] 已添加助手响应消息")

            # 私聊：助手消息写入后也执行消息数量上限控制
            is_group = event.get_message_type() == MessageType.GROUP_MESSAGE
            if not is_group:
                await self.message_utils.enforce_message_limit(session_id)

            # 获取会话信息
            session_info = await self.conversation_manager.get_session_info(session_id)
            if not session_info:
                logger.warning(
                    f"[DEBUG-Reflection] [{session_id}] session_info 为 None，跳过反思"
                )
                return

            # 获取实际消息数量（用于数据一致性检查）
            actual_message_count = (
                await self.conversation_manager.store.get_message_count(session_id)
            )

            # 数据一致性检查
            if session_info.message_count != actual_message_count:
                logger.warning(
                    f"[DEBUG-Reflection] [{session_id}] 数据不一致! "
                    f"sessions表记录={session_info.message_count}, "
                    f"实际消息数={actual_message_count}"
                )

            # 使用实际消息数量
            total_messages = actual_message_count

            # 检查是否满足总结条件
            trigger_rounds = self.config_manager.get(
                "reflection_engine.summary_trigger_rounds", 10
            )

            # 获取上次总结的位置
            last_summarized_index = (
                await self.conversation_manager.get_session_metadata(
                    session_id, "last_summarized_index", 0
                )
            )

            # 检查 last_summarized_index 是否超出实际消息数量
            # 这种情况通常发生在消息被删除后
            if last_summarized_index > total_messages:
                logger.warning(
                    f"[DEBUG-Reflection] [{session_id}] last_summarized_index({last_summarized_index}) "
                    f"> 实际消息数({total_messages})，调整为当前消息总数"
                )
                # 调整为当前消息总数，而非归零（避免重复处理已总结的内容）
                last_summarized_index = total_messages
                await self.conversation_manager.update_session_metadata(
                    session_id, "last_summarized_index", total_messages
                )

            # 计算未总结的消息数量
            unsummarized_messages = total_messages - last_summarized_index
            unsummarized_rounds = unsummarized_messages // 2

            # 检查是否有待处理的失败总结
            pending_summary = await self.conversation_manager.get_session_metadata(
                session_id, "pending_summary", None
            )

            logger.info(
                f"[DEBUG-Reflection] [{session_id}] 总消息数: {total_messages}, "
                f"上次总结位置: {last_summarized_index}, "
                f"未总结轮数: {unsummarized_rounds}, "
                f"触发阈值: {trigger_rounds}轮, "
                f"待处理失败总结: {pending_summary is not None}"
            )

            # 当未总结的轮数达到触发阈值时进行总结
            if unsummarized_rounds >= trigger_rounds:
                logger.info(
                    f"[{session_id}] 未总结轮数达到 {unsummarized_rounds} 轮，启动记忆反思任务"
                )

                # 计算总结范围（考虑待处理的失败总结）
                start_index = last_summarized_index
                end_index = total_messages
                retry_count = 0

                # 如果有待处理的失败总结，合并范围
                if pending_summary:
                    pending_start = pending_summary.get("start_index", start_index)
                    retry_count = pending_summary.get("retry_count", 0)

                    # 检查是否已达到最大重试次数
                    if retry_count >= 3:
                        logger.warning(
                            f"[{session_id}] 待处理总结已连续失败 {retry_count} 次，放弃该范围 "
                            f"[{pending_start}:{pending_summary.get('end_index', end_index)}]"
                        )
                        # 清除待处理记录，更新 last_summarized_index 到当前位置
                        await self.conversation_manager.update_session_metadata(
                            session_id, "pending_summary", None
                        )
                        await self.conversation_manager.update_session_metadata(
                            session_id, "last_summarized_index", end_index
                        )
                        return

                    # 合并范围：使用待处理的起始位置
                    start_index = pending_start
                    logger.info(
                        f"[{session_id}] 合并待处理失败总结，新范围 [{start_index}:{end_index}], "
                        f"重试次数: {retry_count + 1}/3"
                    )

                if end_index - start_index < 2:
                    logger.debug(f"[{session_id}] 消息数不足一轮对话，跳过总结")
                    return

                messages_to_summarize = end_index - start_index
                rounds_to_summarize = messages_to_summarize // 2

                logger.info(
                    f"[{session_id}] 滑动窗口总结: "
                    f"消息范围 [{start_index}:{end_index}]/{total_messages}, "
                    f"本次总结 {rounds_to_summarize} 轮"
                )

                # 获取需要总结的消息
                history_messages = await self.conversation_manager.get_messages_range(
                    session_id=session_id, start_index=start_index, end_index=end_index
                )

                logger.info(
                    f"[{session_id}] 获取到 {len(history_messages)} 条消息用于总结"
                )

                persona_id = await get_persona_id(self.context, event)
                summary_identity = build_summary_identity(
                    event,
                    session_id=session_id,
                    is_group=is_group,
                    history_messages=history_messages,
                )

                # 创建后台任务进行存储（跟踪任务）
                if not self._shutting_down:
                    async with self._storage_state_lock:
                        if session_id in self._storage_sessions_inflight:
                            logger.info(
                                f"[{session_id}] 已有记忆反思任务在执行，跳过本次触发"
                            )
                            return
                        self._storage_sessions_inflight.add(session_id)

                    try:
                        task = asyncio.create_task(
                            self._storage_task(
                                session_id,
                                history_messages,
                                persona_id,
                                start_index,
                                end_index,
                                retry_count,
                                summary_identity,
                            )
                        )
                    except Exception:
                        self._storage_sessions_inflight.discard(session_id)
                        raise

                    self._storage_tasks.add(task)
                    task.add_done_callback(
                        lambda t, sid=session_id: self._on_storage_task_done(t, sid)
                    )

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"处理记忆反思时发生错误: {e}", exc_info=True)

    async def _storage_task(
        self,
        session_id: str,
        history_messages: list[dict],
        persona_id: str,
        start_index: int,
        end_index: int,
        retry_count: int,
        summary_identity: dict[str, Any] | None = None,
    ):
        """后台存储任务"""
        from ..utils import OperationContext

        async with OperationContext("记忆存储", session_id):
            try:
                # 即使本总结任务已过期，也先重试历史对象双写；重试请求持有
                # 已成功 legacy 文档的 ID，因此绝不会重复写 legacy。
                await retry_pending_summary_key_facts(self.memory_engine)

                # 如果其他任务已经推进了总结进度，本任务可能已过期，直接跳过
                current_summarized = (
                    await self.conversation_manager.get_session_metadata(
                        session_id, "last_summarized_index", 0
                    )
                )
                try:
                    summarized_index = int(current_summarized)
                except (TypeError, ValueError):
                    summarized_index = 0

                if summarized_index >= end_index:
                    logger.info(
                        f"[{session_id}] 检测到过期总结任务，跳过: "
                        f"current={summarized_index}, target_end={end_index}"
                    )
                    return

                # 判断是否为群聊
                is_group_chat = bool(
                    history_messages[0].group_id if history_messages else False
                )
                # 备用判断：从 session_id 解析（防御性编程）
                if not is_group_chat and "GroupMessage" in session_id:
                    is_group_chat = True

                logger.info(
                    f"[{session_id}] 开始处理记忆，类型={'群聊' if is_group_chat else '私聊'}, "
                    f"范围=[{start_index}:{end_index}], 重试次数={retry_count}, "
                    f"当前人格={persona_id or '未设置'}"
                )

                # 使用 MemoryProcessor 处理对话历史
                if not self.memory_processor:
                    logger.error(f"[{session_id}] MemoryProcessor 未初始化，记录待重试")
                    await self._record_pending_summary(
                        session_id, start_index, end_index, retry_count
                    )
                    return

                try:
                    logger.info(
                        f"[{session_id}] 调用 MemoryProcessor 处理 {len(history_messages)} 条消息"
                    )
                    (
                        content,
                        metadata,
                        importance,
                    ) = await self.memory_processor.process_conversation(
                        messages=history_messages,
                        is_group_chat=is_group_chat,
                        persona_id=persona_id,
                    )

                    atoms = self.memory_processor.classify_atoms_from_metadata(
                        metadata=metadata,
                        parent_importance=importance,
                        session_id=session_id,
                        persona_id=persona_id,
                    )

                    # 补充 source_window 元数据，记录索引范围与真实消息 ID。
                    message_start_id, message_end_id = get_summary_message_id_range(
                        history_messages
                    )
                    metadata["source_window"] = {
                        "session_id": session_id,
                        "start_index": start_index,
                        "end_index": end_index,
                        "message_count": end_index - start_index,
                        "message_start_id": message_start_id,
                        "message_end_id": message_end_id,
                        "sender_ids": list(
                            dict.fromkeys(
                                str(getattr(message, "sender_id", "") or "").strip()
                                for message in history_messages
                                if str(
                                    getattr(message, "sender_id", "") or ""
                                ).strip()
                            )
                        ),
                        **(summary_identity or {}),
                    }

                    logger.info(
                        f"[{session_id}] 已使用LLM生成结构化记忆, "
                        f"主题={metadata.get('topics', [])}, "
                        f"重要性={importance:.2f}"
                    )

                except Exception as e:
                    # LLM处理失败，记录待重试信息
                    logger.error(
                        f"[{session_id}] LLM处理失败 (重试 {retry_count + 1}/3): {e}",
                        exc_info=True,
                    )
                    await self._record_pending_summary(
                        session_id, start_index, end_index, retry_count
                    )
                    return

                # 正常流程：先写 legacy 文档，再尽力双写 owner-scoped key_facts。
                if self.memory_engine:
                    legacy_document_id = await self.memory_engine.add_memory(
                        content=content,
                        session_id=session_id,
                        persona_id=persona_id,
                        importance=importance,
                        metadata=metadata,
                        atoms=atoms,
                    )

                    logger.info(
                        f"[{session_id}] 成功存储对话记忆（{len(history_messages)}条消息，重要性={importance:.2f}）"
                    )
                    feedback_stats = await persist_summary_key_facts(
                        memory_engine=self.memory_engine,
                        identity=summary_identity,
                        history_messages=history_messages,
                        persona_id=persona_id,
                        metadata=metadata,
                        legacy_document_id=legacy_document_id,
                        importance=importance,
                        triggered_by="memory-reflection",
                    )
                    if feedback_stats["failed"] or feedback_stats["errors"]:
                        logger.warning(
                            f"[{session_id}] legacy 总结已保存，但 key_facts 双写未完全成功: "
                            f"created={feedback_stats['created']}, "
                            f"deduplicated={feedback_stats['deduplicated']}, "
                            f"failed={feedback_stats['failed']}, "
                            f"skipped={feedback_stats['skipped']}, "
                            f"errors={feedback_stats['errors']}"
                        )
                    elif feedback_stats["attempted"]:
                        logger.info(
                            f"[{session_id}] key_facts 双写完成: "
                            f"created={feedback_stats['created']}, "
                            f"deduplicated={feedback_stats['deduplicated']}"
                        )

                # 成功：更新已总结的位置，清除待处理记录
                if self.conversation_manager:
                    try:
                        await self.conversation_manager.update_session_metadata(
                            session_id, "last_summarized_index", end_index
                        )
                        await self.conversation_manager.update_session_metadata(
                            session_id, "pending_summary", None
                        )
                        logger.info(
                            f"[{session_id}] 更新滑动窗口位置: last_summarized_index = {end_index}"
                        )
                    except Exception as meta_err:
                        logger.error(
                            f"[{session_id}] 记忆已存储但元数据更新失败: {meta_err}。"
                            "下次触发时将跳过本段消息，避免重复总结。",
                            exc_info=True,
                        )
                        # Advance the index anyway to prevent re-processing the
                        # same message range (memory is already stored durably).
                        try:
                            await self.conversation_manager.update_session_metadata(
                                session_id, "last_summarized_index", end_index
                            )
                            await self.conversation_manager.update_session_metadata(
                                session_id, "pending_summary", None
                            )
                        except Exception:
                            logger.error(
                                f"[{session_id}] 重试元数据更新仍然失败，"
                                "可能出现重复总结。",
                                exc_info=True,
                            )

            except Exception as e:
                logger.error(f"[{session_id}] 存储记忆失败: {e}", exc_info=True)
                await self._record_pending_summary(
                    session_id, start_index, end_index, retry_count
                )

    async def _record_pending_summary(
        self,
        session_id: str,
        start_index: int,
        end_index: int,
        current_retry_count: int,
    ):
        """记录待处理的失败总结信息"""
        if not self.conversation_manager:
            return

        new_retry_count = current_retry_count + 1
        pending_summary = {
            "start_index": start_index,
            "end_index": end_index,
            "retry_count": new_retry_count,
        }

        await self.conversation_manager.update_session_metadata(
            session_id, "pending_summary", pending_summary
        )

        logger.warning(
            f"[{session_id}] 记录待重试总结: 范围=[{start_index}:{end_index}], "
            f"重试次数={new_retry_count}/3"
        )

    def _on_storage_task_done(self, task: asyncio.Task, session_id: str):
        """存储任务完成回调"""
        self._storage_tasks.discard(task)
        self._storage_sessions_inflight.discard(session_id)

        if task.cancelled():
            logger.info(f"[{session_id}] 存储任务已取消")
            return

        exc = task.exception()
        if exc:
            logger.error(f"[{session_id}] 存储任务异常: {exc}", exc_info=exc)

    def set_shutting_down(self, value: bool):
        """设置关闭状态"""
        self._shutting_down = value
