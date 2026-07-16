"""Strict models for owner-scoped, revisioned evolving memory objects."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, StrictStr, field_validator, model_validator


MAX_IDENTIFIER_LENGTH = 512
MAX_CONTENT_LENGTH = 65536
MAX_REASON_LENGTH = 4096


class MemoryScope(str, Enum):
    USER = "user"
    PERSONA = "persona"
    SESSION = "session"
    PUBLIC = "public"
    LEGACY_SESSION = "legacy_session"


class MemoryItemStatus(str, Enum):
    ACTIVE = "active"
    CONFLICTED = "conflicted"
    ARCHIVED = "archived"
    SUPERSEDED = "superseded"


class MemoryAction(str, Enum):
    CREATE = "create"
    UPDATE = "update"
    MERGE = "merge"
    SUPERSEDE = "supersede"
    ARCHIVE = "archive"


class RevisionOperation(str, Enum):
    CREATE = "create"
    UPDATE = "update"
    MERGE = "merge"
    SUPERSEDE = "supersede"
    ARCHIVE = "archive"
    BACKFILL = "backfill"
    CONFLICT = "conflict"


class MemoryActorType(str, Enum):
    AUTOMATIC = "automatic"
    ADMIN = "admin"
    USER = "user"
    MIGRATION = "migration"
    SYSTEM = "system"


class MemoryRelationType(str, Enum):
    MERGED_INTO = "merged_into"
    SUPERSEDES = "supersedes"
    DERIVED_FROM = "derived_from"
    DUPLICATE_OF = "duplicate_of"
    CONFLICTS_WITH = "conflicts_with"
    RELATED_TO = "related_to"


class ConflictStatus(str, Enum):
    OPEN = "open"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


class ConflictSeverity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class IndexStatus(str, Enum):
    CURRENT = "current"
    PENDING = "pending"
    NEEDS_REPAIR = "needs_repair"
    DISABLED = "disabled"


class SourceAvailability(str, Enum):
    AVAILABLE = "available"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"


class IdentityLinkStatus(str, Enum):
    ACTIVE = "active"
    REVOKED = "revoked"


class OwnerStatus(str, Enum):
    ACTIVE = "active"
    MERGED = "merged"
    DISABLED = "disabled"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class StrictMemoryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    @field_validator("*", mode="before")
    @classmethod
    def reject_nul_strings(cls, value: Any) -> Any:
        if isinstance(value, str) and "\x00" in value:
            raise ValueError("字符串不得包含 NUL 字符")
        return value


class MemoryAccessContext(StrictMemoryModel):
    owner_user_id: StrictStr = Field(min_length=1, max_length=MAX_IDENTIFIER_LENGTH)
    platform_id: StrictStr = Field(min_length=1, max_length=MAX_IDENTIFIER_LENGTH)
    bot_id: StrictStr = Field(min_length=1, max_length=MAX_IDENTIFIER_LENGTH)
    external_user_id: StrictStr = Field(min_length=1, max_length=MAX_IDENTIFIER_LENGTH)
    session_id: StrictStr = Field(min_length=1, max_length=MAX_IDENTIFIER_LENGTH)
    persona_id: StrictStr | None = Field(default=None, min_length=1, max_length=MAX_IDENTIFIER_LENGTH)
    is_group: StrictBool
    allowed_scopes: frozenset[MemoryScope] = Field(
        default_factory=lambda: frozenset(
            {MemoryScope.USER, MemoryScope.PERSONA, MemoryScope.SESSION}
        )
    )
    allow_public: StrictBool = False
    allow_legacy_session: StrictBool = False

    @field_validator(
        "owner_user_id",
        "platform_id",
        "bot_id",
        "external_user_id",
        "session_id",
        "persona_id",
    )
    @classmethod
    def normalize_identifier(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("标识符不得为空")
        return normalized

    @model_validator(mode="after")
    def validate_scope_capabilities(self) -> "MemoryAccessContext":
        if MemoryScope.PUBLIC in self.allowed_scopes and not self.allow_public:
            raise ValueError("允许 public scope 时必须显式开启 allow_public")
        if MemoryScope.LEGACY_SESSION in self.allowed_scopes and not self.allow_legacy_session:
            raise ValueError("允许 legacy_session 时必须显式开启 allow_legacy_session")
        return self

    def can_access_item(self, item: "MemoryItem") -> bool:
        if item.owner_user_id != self.owner_user_id:
            return False
        if item.scope not in self.allowed_scopes:
            return False
        if item.scope == MemoryScope.PUBLIC:
            return self.allow_public
        if item.scope == MemoryScope.PERSONA and item.persona_id != self.persona_id:
            return False
        if item.scope in {MemoryScope.SESSION, MemoryScope.LEGACY_SESSION}:
            if item.session_id != self.session_id:
                return False
        if self.is_group and item.scope in {MemoryScope.USER, MemoryScope.PERSONA}:
            return item.group_safe
        return True


class MemoryOwner(StrictMemoryModel):
    owner_user_id: StrictStr = Field(min_length=1, max_length=MAX_IDENTIFIER_LENGTH)
    display_name: StrictStr | None = Field(default=None, max_length=512)
    status: OwnerStatus = OwnerStatus.ACTIVE
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: StrictStr = Field(default_factory=utc_now_iso)
    updated_at: StrictStr = Field(default_factory=utc_now_iso)


class MemoryIdentityLink(StrictMemoryModel):
    identity_link_id: StrictInt | None = None
    owner_user_id: StrictStr = Field(min_length=1, max_length=MAX_IDENTIFIER_LENGTH)
    platform_id: StrictStr = Field(min_length=1, max_length=MAX_IDENTIFIER_LENGTH)
    bot_id: StrictStr = Field(min_length=1, max_length=MAX_IDENTIFIER_LENGTH)
    external_user_id: StrictStr = Field(min_length=1, max_length=MAX_IDENTIFIER_LENGTH)
    verified: StrictBool = False
    source: StrictStr = Field(default="automatic", min_length=1, max_length=128)
    status: IdentityLinkStatus = IdentityLinkStatus.ACTIVE
    created_at: StrictStr = Field(default_factory=utc_now_iso)
    updated_at: StrictStr = Field(default_factory=utc_now_iso)

    @field_validator("owner_user_id", "platform_id", "bot_id", "external_user_id", "source")
    @classmethod
    def normalize_link_identifier(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("身份映射字段不得为空")
        return normalized


class MemoryItem(StrictMemoryModel):
    memory_item_id: StrictStr = Field(min_length=1, max_length=MAX_IDENTIFIER_LENGTH)
    owner_user_id: StrictStr = Field(min_length=1, max_length=MAX_IDENTIFIER_LENGTH)
    scope: MemoryScope
    session_id: StrictStr | None = Field(default=None, max_length=MAX_IDENTIFIER_LENGTH)
    persona_id: StrictStr | None = Field(default=None, max_length=MAX_IDENTIFIER_LENGTH)
    item_type: StrictStr = Field(default="fact", min_length=1, max_length=128)
    canonical_key: StrictStr = Field(min_length=1, max_length=2048)
    content_hash: StrictStr = Field(min_length=16, max_length=128)
    status: MemoryItemStatus = MemoryItemStatus.ACTIVE
    current_revision_no: StrictInt = Field(ge=1)
    version: StrictInt = Field(ge=1)
    current_document_id: StrictInt | None = Field(default=None, ge=1)
    importance: StrictFloat = Field(default=0.5, ge=0.0, le=1.0)
    confidence: StrictFloat = Field(default=0.7, ge=0.0, le=1.0)
    useful_score: StrictFloat = Field(default=0.0, ge=-1.0, le=1.0)
    useful_count: StrictInt = Field(default=0, ge=0)
    invalid_count: StrictInt = Field(default=0, ge=0)
    group_safe: StrictBool = False
    index_status: IndexStatus = IndexStatus.PENDING
    index_error: StrictStr | None = Field(default=None, max_length=2000)
    content: StrictStr = Field(min_length=1, max_length=MAX_CONTENT_LENGTH)
    structured_payload: dict[str, Any] = Field(default_factory=dict)
    created_at: StrictStr = Field(default_factory=utc_now_iso)
    updated_at: StrictStr = Field(default_factory=utc_now_iso)

    @field_validator(
        "memory_item_id",
        "owner_user_id",
        "session_id",
        "persona_id",
        "item_type",
        "canonical_key",
        "content",
    )
    @classmethod
    def normalize_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("文本字段不得为空")
        return normalized

    @model_validator(mode="after")
    def validate_scope_fields(self) -> "MemoryItem":
        if self.scope == MemoryScope.PERSONA and not self.persona_id:
            raise ValueError("persona scope 必须提供 persona_id")
        if self.scope in {MemoryScope.SESSION, MemoryScope.LEGACY_SESSION} and not self.session_id:
            raise ValueError(f"{self.scope.value} scope 必须提供 session_id")
        return self


class MemoryRevision(StrictMemoryModel):
    revision_id: StrictStr = Field(min_length=1, max_length=MAX_IDENTIFIER_LENGTH)
    memory_item_id: StrictStr = Field(min_length=1, max_length=MAX_IDENTIFIER_LENGTH)
    owner_user_id: StrictStr = Field(min_length=1, max_length=MAX_IDENTIFIER_LENGTH)
    revision_no: StrictInt = Field(ge=1)
    operation: RevisionOperation
    content: StrictStr = Field(min_length=1, max_length=MAX_CONTENT_LENGTH)
    content_hash: StrictStr = Field(min_length=16, max_length=128)
    structured_payload: dict[str, Any] = Field(default_factory=dict)
    base_version: StrictInt = Field(ge=0)
    actor_type: MemoryActorType
    actor_id: StrictStr = Field(min_length=1, max_length=MAX_IDENTIFIER_LENGTH)
    reason: StrictStr | None = Field(default=None, max_length=MAX_REASON_LENGTH)
    created_at: StrictStr = Field(default_factory=utc_now_iso)


class MemorySource(StrictMemoryModel):
    source_id: StrictStr = Field(min_length=1, max_length=MAX_IDENTIFIER_LENGTH)
    source_key: StrictStr = Field(min_length=1, max_length=2048)
    owner_user_id: StrictStr = Field(min_length=1, max_length=MAX_IDENTIFIER_LENGTH)
    memory_item_id: StrictStr = Field(min_length=1, max_length=MAX_IDENTIFIER_LENGTH)
    revision_no: StrictInt = Field(ge=1)
    source_type: StrictStr = Field(min_length=1, max_length=128)
    source_ref: StrictStr | None = Field(default=None, max_length=2048)
    document_id: StrictInt | None = Field(default=None, ge=1)
    session_id: StrictStr | None = Field(default=None, max_length=MAX_IDENTIFIER_LENGTH)
    message_start_id: StrictInt | None = Field(default=None, ge=1)
    message_end_id: StrictInt | None = Field(default=None, ge=1)
    content_snapshot: StrictStr | None = Field(default=None, max_length=MAX_CONTENT_LENGTH)
    availability: SourceAvailability = SourceAvailability.AVAILABLE
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: StrictStr = Field(default_factory=utc_now_iso)

    @model_validator(mode="after")
    def validate_message_range(self) -> "MemorySource":
        if (
            self.message_start_id is not None
            and self.message_end_id is not None
            and self.message_start_id > self.message_end_id
        ):
            raise ValueError("message_start_id 不得大于 message_end_id")
        return self


class MemoryRelation(StrictMemoryModel):
    relation_id: StrictStr = Field(min_length=1, max_length=MAX_IDENTIFIER_LENGTH)
    owner_user_id: StrictStr = Field(min_length=1, max_length=MAX_IDENTIFIER_LENGTH)
    source_item_id: StrictStr = Field(min_length=1, max_length=MAX_IDENTIFIER_LENGTH)
    target_item_id: StrictStr = Field(min_length=1, max_length=MAX_IDENTIFIER_LENGTH)
    relation_type: MemoryRelationType
    source_revision_no: StrictInt | None = Field(default=None, ge=1)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: StrictStr = Field(default_factory=utc_now_iso)

    @model_validator(mode="after")
    def reject_self_relation(self) -> "MemoryRelation":
        if self.source_item_id == self.target_item_id:
            raise ValueError("记忆关系两端不得相同")
        return self


class MemoryConflict(StrictMemoryModel):
    conflict_id: StrictStr = Field(min_length=1, max_length=MAX_IDENTIFIER_LENGTH)
    owner_user_id: StrictStr = Field(min_length=1, max_length=MAX_IDENTIFIER_LENGTH)
    left_item_id: StrictStr = Field(min_length=1, max_length=MAX_IDENTIFIER_LENGTH)
    right_item_id: StrictStr = Field(min_length=1, max_length=MAX_IDENTIFIER_LENGTH)
    conflict_type: StrictStr = Field(min_length=1, max_length=128)
    severity: ConflictSeverity = ConflictSeverity.MEDIUM
    status: ConflictStatus = ConflictStatus.OPEN
    resolution_action: StrictStr | None = Field(default=None, max_length=128)
    resolved_by: StrictStr | None = Field(default=None, max_length=MAX_IDENTIFIER_LENGTH)
    resolution_note: StrictStr | None = Field(default=None, max_length=MAX_REASON_LENGTH)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: StrictStr = Field(default_factory=utc_now_iso)
    updated_at: StrictStr = Field(default_factory=utc_now_iso)
    resolved_at: StrictStr | None = None

    @model_validator(mode="after")
    def reject_self_conflict(self) -> "MemoryConflict":
        if self.left_item_id == self.right_item_id:
            raise ValueError("冲突对象两端不得相同")
        return self


class MemoryFeedback(StrictMemoryModel):
    memory_item_id: StrictStr = Field(min_length=1, max_length=MAX_IDENTIFIER_LENGTH)
    expected_version: StrictInt = Field(ge=1)
    useful: StrictBool
    score_delta: StrictFloat = Field(default=0.1, ge=0.0, le=1.0)
    actor_type: MemoryActorType = MemoryActorType.AUTOMATIC
    actor_id: StrictStr = Field(min_length=1, max_length=MAX_IDENTIFIER_LENGTH)
    operation_key: StrictStr = Field(min_length=1, max_length=512)
    reason: StrictStr | None = Field(default=None, max_length=MAX_REASON_LENGTH)


class MutationResult(StrictMemoryModel):
    action: MemoryAction
    item: MemoryItem
    affected_item_ids: tuple[StrictStr, ...] = ()
    deduplicated: StrictBool = False
    operation_key: StrictStr = Field(min_length=1, max_length=512)
    projection_status: IndexStatus = IndexStatus.PENDING
    duplicate_candidates: tuple[StrictStr, ...] = ()


class DuplicateCandidate(StrictMemoryModel):
    item: MemoryItem
    match_type: StrictStr = Field(min_length=1, max_length=32)
    score: StrictFloat = Field(ge=0.0, le=1.0)


__all__ = [
    "ConflictSeverity",
    "ConflictStatus",
    "DuplicateCandidate",
    "IdentityLinkStatus",
    "IndexStatus",
    "MemoryAccessContext",
    "MemoryAction",
    "MemoryActorType",
    "MemoryConflict",
    "MemoryFeedback",
    "MemoryIdentityLink",
    "MemoryItem",
    "MemoryItemStatus",
    "MemoryOwner",
    "MemoryRelation",
    "MemoryRelationType",
    "MemoryRevision",
    "MemoryScope",
    "MemorySource",
    "MutationResult",
    "OwnerStatus",
    "RevisionOperation",
    "SourceAvailability",
    "utc_now_iso",
]
