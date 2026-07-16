"""Shared access-policy helpers for retrieval routes."""

from __future__ import annotations

import json
from typing import Any

from ..models.evolving_memory import MemoryAccessContext, MemoryScope


def coerce_metadata(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    if isinstance(value, str):
        try:
            loaded = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return {}
        return loaded if isinstance(loaded, dict) else {}
    return {}


def _item_projection_accessible(
    metadata: dict[str, Any], context: MemoryAccessContext
) -> bool:
    if metadata.get("owner_user_id") != context.owner_user_id:
        return False
    if str(metadata.get("projection_status") or "current") == "stale":
        return False
    if str(metadata.get("item_status") or metadata.get("status") or "active") not in {
        "active",
        "conflicted",
    }:
        return False

    try:
        scope = MemoryScope(str(metadata.get("scope") or MemoryScope.SESSION.value))
    except ValueError:
        return False
    if scope not in context.allowed_scopes:
        return False
    if scope == MemoryScope.PUBLIC:
        return context.allow_public
    if scope == MemoryScope.PERSONA:
        if metadata.get("persona_id") != context.persona_id:
            return False
        if context.is_group and not bool(metadata.get("group_safe", False)):
            return False
    elif scope in {MemoryScope.SESSION, MemoryScope.LEGACY_SESSION}:
        if metadata.get("session_id") != context.session_id:
            return False
        if scope == MemoryScope.LEGACY_SESSION and not context.allow_legacy_session:
            return False
    elif scope == MemoryScope.USER and context.is_group:
        if not bool(metadata.get("group_safe", False)):
            return False
    return True


def is_metadata_accessible(
    metadata_value: Any,
    *,
    access_context: MemoryAccessContext | None,
    session_id: str | None = None,
    persona_id: str | None = None,
    include_item_projection: bool = False,
) -> bool:
    """Apply owner/scope policy while preserving legacy filtering semantics.

    Without an access context this keeps the previous session/persona behavior.
    With an access context, owner-tagged legacy rows require an exact owner match;
    rows without an owner are restricted to the current complete session id.
    """

    metadata = coerce_metadata(metadata_value)
    is_item_projection = metadata.get("archive_type") == "memory_item_projection"
    if is_item_projection:
        if not include_item_projection or access_context is None:
            return False
        return _item_projection_accessible(metadata, access_context)

    if str(metadata.get("projection_status") or "current") == "stale":
        return False

    stored_session = metadata.get("session_id")
    stored_persona = metadata.get("persona_id")
    if access_context is None:
        if session_id is not None and stored_session != session_id:
            return False
        if persona_id is not None and stored_persona != persona_id:
            return False
        return True

    stored_owner = str(metadata.get("owner_user_id") or "").strip()
    if stored_owner:
        if stored_owner != access_context.owner_user_id:
            return False
    elif stored_session != access_context.session_id:
        return False

    if session_id is not None and stored_session != session_id:
        return False
    if persona_id is not None and stored_persona != persona_id:
        return False
    return True


__all__ = ["coerce_metadata", "is_metadata_accessible"]
