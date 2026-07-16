"""AstrBot 事件到 owner-scoped 可演化记忆上下文的安全适配。"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger
from astrbot.api.platform import MessageType

from ..models.evolving_memory import MemoryAccessContext

RECALL_TRACE_EXTRA_KEY = "_livingmemory_recall_trace"
RESPONSE_CONTEXT_EXTRA_KEY = "_livingmemory_response_context"


def _safe_text(value: Any) -> str:
    if value is None:
        return ""
    try:
        return str(value).strip()
    except Exception:
        return ""


def _event_call(event: Any, method_name: str) -> Any:
    method = getattr(event, method_name, None)
    if not callable(method):
        return None
    try:
        return method()
    except Exception:
        return None


def _sender_id_from_message(event: Any) -> str:
    message_obj = getattr(event, "message_obj", None)
    sender = getattr(message_obj, "sender", None)
    if isinstance(sender, dict):
        for key in ("user_id", "sender_id", "id"):
            value = _safe_text(sender.get(key))
            if value:
                return value
    else:
        for key in ("user_id", "sender_id", "id"):
            value = _safe_text(getattr(sender, key, None))
            if value:
                return value
    return ""


async def build_access_context_from_event(
    event: Any,
    evolving_manager: Any,
    *,
    astrbot_context: Any | None = None,
    persona_id: str | None = None,
) -> MemoryAccessContext | None:
    """从事件构造完整访问上下文；身份字段不完整时拒绝对象层访问。"""
    if event is None or evolving_manager is None:
        return None

    session_id = _safe_text(getattr(event, "unified_msg_origin", None))
    platform_id = _safe_text(_event_call(event, "get_platform_name"))
    if not platform_id and session_id:
        platform_id = session_id.split(":", 1)[0].strip()

    bot_id = _safe_text(_event_call(event, "get_self_id"))
    if not bot_id:
        bot_id = _safe_text(getattr(getattr(event, "message_obj", None), "self_id", None))

    external_user_id = _safe_text(_event_call(event, "get_sender_id"))
    if not external_user_id:
        external_user_id = _sender_id_from_message(event)

    message_type = _event_call(event, "get_message_type")
    is_group = message_type == MessageType.GROUP_MESSAGE
    if message_type is None and session_id:
        parts = session_id.split(":", 2)
        is_group = len(parts) == 3 and parts[1] == "GroupMessage"

    if persona_id is None and astrbot_context is not None:
        try:
            from . import get_persona_id

            persona_id = await get_persona_id(astrbot_context, event)
        except Exception:
            persona_id = None

    missing = [
        name
        for name, value in (
            ("platform_id", platform_id),
            ("bot_id", bot_id),
            ("external_user_id", external_user_id),
            ("session_id", session_id),
        )
        if not value
    ]
    if missing:
        logger.warning(
            "可演化记忆访问上下文字段不完整，已禁用本次对象层读写: %s",
            ",".join(missing),
        )
        return None

    try:
        return await evolving_manager.build_access_context(
            platform_id=platform_id,
            bot_id=bot_id,
            external_user_id=external_user_id,
            session_id=session_id,
            persona_id=_safe_text(persona_id) or None,
            is_group=bool(is_group),
        )
    except Exception as exc:
        logger.warning(
            "构造可演化记忆访问上下文失败: %s",
            type(exc).__name__,
            exc_info=True,
        )
        return None


def serialize_access_context(access_context: MemoryAccessContext) -> dict[str, Any]:
    return {
        "owner_user_id": access_context.owner_user_id,
        "platform_id": access_context.platform_id,
        "bot_id": access_context.bot_id,
        "external_user_id": access_context.external_user_id,
        "session_id": access_context.session_id,
        "persona_id": access_context.persona_id,
        "is_group": access_context.is_group,
        "allowed_scopes": sorted(scope.value for scope in access_context.allowed_scopes),
    }


def append_recall_trace(
    event: Any,
    memories: list[Any],
    access_context: MemoryAccessContext | None,
    *,
    recall_source: str,
) -> list[dict[str, Any]]:
    """把实际使用的对象召回追加到事件 trace；legacy 结果不会进入。"""
    if event is None or access_context is None:
        return []
    context_payload = serialize_access_context(access_context)
    entries: list[dict[str, Any]] = []
    for memory in memories:
        memory_item_id = _safe_text(getattr(memory, "memory_item_id", None))
        if not memory_item_id:
            metadata = getattr(memory, "metadata", None)
            if isinstance(metadata, dict):
                memory_item_id = _safe_text(metadata.get("memory_item_id"))
        if not memory_item_id:
            continue
        metadata = getattr(memory, "metadata", None)
        metadata = metadata if isinstance(metadata, dict) else {}
        version = getattr(memory, "version", None)
        if version is None:
            version = metadata.get("version") or metadata.get("memory_revision_no")
        try:
            version = int(version)
        except (TypeError, ValueError):
            continue
        score = getattr(memory, "final_score", None)
        try:
            score = float(score)
        except (TypeError, ValueError):
            score = 0.0
        entries.append(
            {
                "memory_item_id": memory_item_id,
                "version": version,
                "score": score,
                "source_type": _safe_text(getattr(memory, "source_type", None))
                or "memory_item",
                "content": _safe_text(getattr(memory, "content", None))[:65536],
                "scope": _safe_text(metadata.get("scope")),
                "recall_source": recall_source,
                "context": context_payload,
                "access_context": context_payload,
            }
        )
    if not entries:
        return []

    get_extra = getattr(event, "get_extra", None)
    existing = get_extra(RECALL_TRACE_EXTRA_KEY, []) if callable(get_extra) else []
    trace = list(existing) if isinstance(existing, list) else []
    seen = {
        (str(item.get("memory_item_id")), int(item.get("version", 0) or 0))
        for item in trace
        if isinstance(item, dict)
    }
    for entry in entries:
        key = (entry["memory_item_id"], entry["version"])
        if key not in seen:
            trace.append(entry)
            seen.add(key)
    set_extra = getattr(event, "set_extra", None)
    if callable(set_extra):
        set_extra(RECALL_TRACE_EXTRA_KEY, trace)
    else:
        setattr(event, RECALL_TRACE_EXTRA_KEY, trace)
    return entries


__all__ = [
    "RECALL_TRACE_EXTRA_KEY",
    "RESPONSE_CONTEXT_EXTRA_KEY",
    "append_recall_trace",
    "build_access_context_from_event",
    "serialize_access_context",
]
