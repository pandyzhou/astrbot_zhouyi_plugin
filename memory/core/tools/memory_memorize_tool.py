"""供 Agent 主动调用的长期记忆写入工具。"""

import asyncio
import hashlib
import json
from dataclasses import field
from typing import Any

from pydantic.dataclasses import dataclass

from astrbot.api import logger
from astrbot.api.platform import MessageType
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext

from ..models.evolving_memory import IndexStatus, MemoryActorType, MemoryScope
from ..utils import build_access_context_from_event, get_persona_id


def _json_result(data: dict[str, Any]) -> str:
    """将工具结果稳定序列化为 JSON 文本。"""
    return json.dumps(data, ensure_ascii=False, default=str)


def _normalize_list(value: Any, limit: int = 5) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()][:limit]
    if isinstance(value, str) and value.strip():
        return [value.strip()][:limit]
    return []


@dataclass
class MemoryMemorizeTool(FunctionTool[AstrAgentContext]):
    """长期记忆主动写入工具。"""

    __pydantic_config__ = {"arbitrary_types_allowed": True}

    context: Any = None
    memory_engine: Any = None
    memory_processor: Any = None
    evolving_memory_manager: Any = None

    name: str = "memorize_long_term_memory"
    description: str = (
        "Memorize durable long-term memory when the user explicitly asks to remember something, "
        "or when stable preferences, identity details, agreements, or project context appear. "
        "Write concise factual memory, not the full conversation."
    )
    parameters: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "memory": {
                    "type": "string",
                    "description": "Concise factual long-term memory to save. Do not copy the full conversation.",
                },
                "topics": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional short topic tags for this memory, up to 5.",
                    "default": [],
                },
                "key_facts": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional key facts supporting the memory, up to 5.",
                    "default": [],
                },
                "sentiment": {
                    "type": "string",
                    "description": "Sentiment of the memory: positive, neutral, or negative.",
                    "default": "neutral",
                },
                "importance": {
                    "type": "number",
                    "description": "Importance from 0.0 to 1.0. Use higher values for durable preferences, commitments, or identity facts.",
                    "default": 0.7,
                },
                "reason": {
                    "type": "string",
                    "description": "Optional short reason why this information should be remembered.",
                    "default": "",
                },
            },
            "required": ["memory"],
        }
    )

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        memory: str,
        topics: list[str] | None = None,
        key_facts: list[str] | None = None,
        sentiment: str = "neutral",
        importance: float = 0.7,
        reason: str = "",
    ) -> ToolExecResult:
        """执行长期记忆写入。"""
        cleaned_memory = (memory or "").strip()
        if not cleaned_memory:
            return _json_result({"memorized": False, "error": "memory is empty"})

        normalized_sentiment = str(sentiment or "neutral").strip().lower()
        if normalized_sentiment not in {"positive", "neutral", "negative"}:
            normalized_sentiment = "neutral"

        if (
            self.context is None
            or self.memory_engine is None
            or self.memory_processor is None
        ):
            return _json_result(
                {
                    "memorized": False,
                    "error": "memory memorize tool is not initialized",
                }
            )

        try:
            event = context.context.event
            session_id = event.unified_msg_origin
            persona_id = await get_persona_id(self.context, event)
            is_group_chat = event.get_message_type() == MessageType.GROUP_MESSAGE

            structured_data = {
                "summary": cleaned_memory,
                "topics": _normalize_list(topics),
                "key_facts": _normalize_list(key_facts),
                "sentiment": normalized_sentiment,
                "importance": importance,
            }

            content, metadata, normalized_importance = (
                self.memory_processor.build_memory_from_structured_data(
                    structured_data=structured_data,
                    is_group_chat=is_group_chat,
                    fallback_excerpt=cleaned_memory,
                )
            )
            metadata["source_window"] = {
                "session_id": session_id,
                "triggered_by": "agent_tool",
                "tool_name": self.name,
            }
            metadata["memory_origin"] = "agent_memorize_tool"
            cleaned_reason = (reason or "").strip()
            if cleaned_reason:
                metadata["memorize_reason"] = cleaned_reason

            manager = self.evolving_memory_manager or getattr(
                self.memory_engine, "evolving_memory_manager", None
            )
            memory_id = None
            memory_item_id = None
            version = None
            deduplicated = False
            projection_status = None
            projection_error = None
            if manager is not None:
                access_context = await build_access_context_from_event(
                    event,
                    manager,
                    astrbot_context=self.context,
                    persona_id=persona_id,
                )
                if access_context is None:
                    return _json_result(
                        {
                            "memorized": False,
                            "error": "access_context_unavailable",
                        }
                    )
                raw_message_id = getattr(
                    getattr(event, "message_obj", None), "message_id", None
                )
                try:
                    source_message_id = int(raw_message_id)
                    if source_message_id <= 0:
                        source_message_id = None
                except (TypeError, ValueError):
                    source_message_id = None
                effective_scope = (
                    MemoryScope.SESSION
                    if is_group_chat
                    else (
                        MemoryScope.PERSONA
                        if access_context.persona_id
                        else MemoryScope.USER
                    )
                )
                structured_payload = {
                    "topics": metadata.get("topics", []),
                    "key_facts": metadata.get("key_facts", []),
                    "sentiment": metadata.get("sentiment", "neutral"),
                    "memorize_reason": cleaned_reason or None,
                }
                operation_context = {
                    "owner_user_id": access_context.owner_user_id,
                    "persona_id": access_context.persona_id,
                    "scope": effective_scope.value,
                    "session_id": access_context.session_id,
                    "content": content,
                    "source_content": cleaned_memory,
                    "structured_payload": structured_payload,
                    "importance": float(normalized_importance),
                }
                content_hash = hashlib.sha256(
                    json.dumps(
                        operation_context,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                operation_key = f"agent-memorize:{content_hash}"
                result = await manager.create(
                    context=access_context,
                    content=content,
                    operation_key=operation_key,
                    scope=effective_scope,
                    structured_payload=structured_payload,
                    importance=normalized_importance,
                    confidence=0.9,
                    actor_type=MemoryActorType.AUTOMATIC,
                    actor_id=self.name,
                    reason=cleaned_reason or "agent memorize tool",
                    source={
                        "source_key": f"agent-memorize:{content_hash}",
                        "source_type": "agent_memorize_tool",
                        "source_ref": self.name,
                        "session_id": access_context.session_id,
                        "message_start_id": source_message_id,
                        "message_end_id": source_message_id,
                        "content_snapshot": cleaned_memory[:65536],
                        "metadata": {"tool_name": self.name},
                    },
                )
                memory_id = result.item.current_document_id
                memory_item_id = result.item.memory_item_id
                version = result.item.version
                deduplicated = result.deduplicated
                projection_status = result.projection_status.value
                projection_error = result.item.index_error
            else:
                # 仅在对象管理器不存在的旧运行环境中兼容 legacy 写入。
                memory_id = await self.memory_engine.add_memory(
                    content=content,
                    session_id=session_id,
                    persona_id=persona_id,
                    importance=normalized_importance,
                    metadata=metadata,
                )

            needs_repair = projection_status == IndexStatus.NEEDS_REPAIR.value
            return _json_result(
                {
                    "memorized": True,
                    "status": "partial_success" if needs_repair else "success",
                    "needs_repair": needs_repair,
                    "projection_status": projection_status,
                    "projection_error": projection_error if needs_repair else None,
                    "id": memory_id,
                    "memory_item_id": memory_item_id,
                    "version": version,
                    "deduplicated": deduplicated,
                    "content": content,
                    "importance": normalized_importance,
                    "session_id": session_id,
                    "persona_id": persona_id,
                }
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"记忆工具写入失败: {e}", exc_info=True)
            return _json_result({"memorized": False, "error": "internal_error"})
