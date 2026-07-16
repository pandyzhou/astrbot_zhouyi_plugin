"""Page API response adapters for evolving memory models."""

from __future__ import annotations

from typing import Any

from ..models.evolving_memory import (
    MemoryIdentityLink,
    MemoryItem,
    MemoryOwner,
    MemoryRelation,
    MemoryRevision,
    MemorySource,
)
from .utils import PageApiUtils


def memory_object_payload(
    item: MemoryItem,
    *,
    owner_display_name: str | None = None,
    counts: dict[str, int] | None = None,
) -> dict[str, Any]:
    counts = counts or {}
    index_status = "synced" if item.index_status.value == "current" else item.index_status.value
    return {
        "memory_item_id": item.memory_item_id,
        "owner_user_id": item.owner_user_id,
        "owner_display_name": owner_display_name,
        "scope": item.scope.value,
        "session_id": item.session_id,
        "persona_id": item.persona_id,
        "item_type": item.item_type,
        "memory_type": item.item_type,
        "canonical_key": item.canonical_key,
        "status": item.status.value,
        "content": item.content,
        "structured_payload": dict(item.structured_payload),
        "current_revision_no": item.current_revision_no,
        "version": item.version,
        "importance": item.importance,
        "confidence": item.confidence,
        "useful_score": item.useful_score,
        "useful_count": item.useful_count,
        "invalid_count": item.invalid_count,
        "group_safe": item.group_safe,
        "current_document_id": item.current_document_id,
        "index_status": index_status,
        "core_index_status": item.index_status.value,
        "index_error": item.index_error,
        "conflict_count": int(counts.get("conflict_count", 0)),
        "source_count": int(counts.get("source_count", 0)),
        "relation_count": int(counts.get("relation_count", 0)),
        "created_at": PageApiUtils.unix_timestamp(item.created_at),
        "updated_at": PageApiUtils.unix_timestamp(item.updated_at),
    }


def revision_payload(revision: MemoryRevision) -> dict[str, Any]:
    actor = revision.actor_id
    if revision.actor_type.value != "admin":
        actor = f"{revision.actor_type.value}:{revision.actor_id}"
    return {
        "revision_id": revision.revision_id,
        "memory_item_id": revision.memory_item_id,
        "revision_no": revision.revision_no,
        "operation": revision.operation.value,
        "content": revision.content,
        "structured_payload": dict(revision.structured_payload),
        "base_version": revision.base_version,
        "actor": actor,
        "actor_type": revision.actor_type.value,
        "actor_id": revision.actor_id,
        "reason": revision.reason,
        "created_at": PageApiUtils.unix_timestamp(revision.created_at),
    }


def source_payload(source: MemorySource) -> dict[str, Any]:
    platform_id = source.metadata.get("platform_id")
    return {
        "source_id": source.source_id,
        "source_key": source.source_key,
        "memory_item_id": source.memory_item_id,
        "revision_no": source.revision_no,
        "source_type": source.source_type,
        "source_ref": source.source_ref,
        "document_id": source.document_id,
        "message_id_start": str(source.message_start_id) if source.message_start_id is not None else None,
        "message_id_end": str(source.message_end_id) if source.message_end_id is not None else None,
        "message_start_id": source.message_start_id,
        "message_end_id": source.message_end_id,
        "session_id": source.session_id,
        "platform_id": str(platform_id) if platform_id is not None else None,
        "content_snapshot": source.content_snapshot,
        "availability": source.availability.value,
        "metadata": dict(source.metadata),
        "created_at": PageApiUtils.unix_timestamp(source.created_at),
    }


def relation_payload(
    relation: MemoryRelation,
    *,
    target_content: str | None = None,
) -> dict[str, Any]:
    return {
        "relation_id": relation.relation_id,
        "relation_type": relation.relation_type.value,
        "source_memory_item_id": relation.source_item_id,
        "target_memory_item_id": relation.target_item_id,
        "source_item_id": relation.source_item_id,
        "target_item_id": relation.target_item_id,
        "target_content": target_content,
        "source_revision_no": relation.source_revision_no,
        "metadata": dict(relation.metadata),
        "created_at": PageApiUtils.unix_timestamp(relation.created_at),
    }


def identity_alias_payload(link: MemoryIdentityLink) -> dict[str, Any]:
    return {
        "identity_link_id": str(link.identity_link_id) if link.identity_link_id is not None else "",
        "owner_user_id": link.owner_user_id,
        "platform_id": link.platform_id,
        "bot_id": link.bot_id,
        "external_user_id": link.external_user_id,
        "verified": link.verified,
        "source": link.source,
        "status": link.status.value,
        "created_at": PageApiUtils.unix_timestamp(link.created_at),
        "updated_at": PageApiUtils.unix_timestamp(link.updated_at),
    }


def owner_payload(owner: MemoryOwner, aliases: list[MemoryIdentityLink]) -> dict[str, Any]:
    return {
        "owner_user_id": owner.owner_user_id,
        "display_name": owner.display_name or owner.owner_user_id,
        "status": owner.status.value,
        "aliases": [identity_alias_payload(link) for link in aliases],
        "metadata": dict(owner.metadata),
        "created_at": PageApiUtils.unix_timestamp(owner.created_at),
        "updated_at": PageApiUtils.unix_timestamp(owner.updated_at),
        "expected_updated_at": owner.updated_at,
    }


__all__ = [
    "identity_alias_payload",
    "memory_object_payload",
    "owner_payload",
    "relation_payload",
    "revision_payload",
    "source_payload",
]
