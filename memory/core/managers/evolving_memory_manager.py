"""Domain manager for owner-scoped evolving memories."""

from __future__ import annotations

import hashlib
import inspect
import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import aiosqlite

from ...storage.evolving_memory_store import EvolvingMemoryStore
from ..base.exceptions import EvolvingMemoryAccessError, EvolvingMemoryNotFoundError
from ..models.evolving_memory import (
    DuplicateCandidate,
    IndexStatus,
    MemoryAccessContext,
    MemoryAction,
    MemoryActorType,
    MemoryFeedback,
    MemoryIdentityLink,
    MemoryItem,
    MemoryItemStatus,
    MemoryScope,
    MutationResult,
    RevisionOperation,
    SourceAvailability,
)


ProjectionCallback = Callable[[MutationResult], Awaitable[Any] | Any]
_BACKFILL_NAMESPACE = uuid.UUID("772bd3e5-4b27-5940-bf4c-1e64d00e43aa")
_BACKFILL_CHECKPOINT = "evolving_memory_key_facts_v1"


class EvolvingMemoryManager:
    """Apply identity, access, deduplication and lifecycle policy above the store."""

    DEFAULT_IDENTITY_CONFIG: dict[str, Any] = {
        "enabled": True,
        "default_scope": "persona",
        "unmapped_policy": "create_isolated_owner",
        "require_explicit_cross_platform_link": True,
        "allow_public_scope": False,
    }
    DEFAULT_EVOLVING_CONFIG: dict[str, Any] = {
        "enabled": True,
        "read_enabled": True,
        "write_enabled": True,
        "feedback_enabled": True,
        "feedback_trigger_mode": "adaptive",
        "feedback_batch_rounds": 3,
        "feedback_idle_seconds": 300,
        "max_actions_per_batch": 5,
        "min_action_confidence": 0.65,
        "group_private_visibility": "explicit_only",
        "migration_batch_size": 100,
    }

    def __init__(
        self,
        store: EvolvingMemoryStore,
        *,
        identity_resolution: dict[str, Any] | None = None,
        evolving_memory: dict[str, Any] | None = None,
        projection_callback: ProjectionCallback | None = None,
    ):
        self.store = store
        self.identity_config = {
            **self.DEFAULT_IDENTITY_CONFIG,
            **(identity_resolution or {}),
        }
        self.evolving_config = {
            **self.DEFAULT_EVOLVING_CONFIG,
            **(evolving_memory or {}),
        }
        self.projection_callback = projection_callback

    async def initialize(self) -> None:
        await self.store.initialize()

    async def build_access_context(
        self,
        *,
        platform_id: str,
        bot_id: str,
        external_user_id: str,
        session_id: str,
        persona_id: str | None,
        is_group: bool,
        include_public: bool = False,
        internal_legacy_migration: bool = False,
    ) -> MemoryAccessContext:
        create_if_missing = (
            self.identity_config.get("unmapped_policy", "create_isolated_owner")
            == "create_isolated_owner"
        )
        link = await self.store.resolve_identity(
            platform_id=platform_id,
            bot_id=bot_id,
            external_user_id=external_user_id,
            create_if_missing=create_if_missing,
        )
        if link is None:
            raise EvolvingMemoryAccessError("当前身份无法解析为 owner")
        allow_public = bool(
            include_public and self.identity_config.get("allow_public_scope", False)
        )
        allowed = {MemoryScope.USER, MemoryScope.PERSONA, MemoryScope.SESSION}
        if allow_public:
            allowed.add(MemoryScope.PUBLIC)
        if internal_legacy_migration:
            allowed.add(MemoryScope.LEGACY_SESSION)
        return MemoryAccessContext(
            owner_user_id=link.owner_user_id,
            platform_id=platform_id,
            bot_id=bot_id,
            external_user_id=external_user_id,
            session_id=session_id,
            persona_id=persona_id,
            is_group=is_group,
            allowed_scopes=frozenset(allowed),
            allow_public=allow_public,
            allow_legacy_session=internal_legacy_migration,
        )

    async def link_identity(
        self,
        *,
        owner_user_id: str,
        platform_id: str,
        bot_id: str,
        external_user_id: str,
        verified: bool = True,
        actor_source: str = "admin",
    ) -> MemoryIdentityLink:
        return await self.store.link_identity(
            owner_user_id=owner_user_id,
            platform_id=platform_id,
            bot_id=bot_id,
            external_user_id=external_user_id,
            verified=verified,
            source=actor_source,
        )

    async def build_admin_access_context(
        self,
        *,
        owner_user_id: str,
        session_id: str | None = None,
        persona_id: str | None = None,
        allow_legacy_session: bool = True,
    ) -> MemoryAccessContext:
        owner = await self.store.get_owner(owner_user_id)
        if owner is None:
            raise EvolvingMemoryNotFoundError("owner 不存在")
        allowed = {
            MemoryScope.USER,
            MemoryScope.PERSONA,
            MemoryScope.SESSION,
            MemoryScope.PUBLIC,
        }
        if allow_legacy_session:
            allowed.add(MemoryScope.LEGACY_SESSION)
        return MemoryAccessContext(
            owner_user_id=owner_user_id,
            platform_id="internal-admin",
            bot_id="dashboard",
            external_user_id=owner_user_id,
            session_id=(session_id or f"internal-admin:{owner_user_id}"),
            persona_id=persona_id,
            is_group=False,
            allowed_scopes=frozenset(allowed),
            allow_public=True,
            allow_legacy_session=allow_legacy_session,
        )

    def _require_write_enabled(self) -> None:
        if not self.evolving_config.get("enabled", True) or not self.evolving_config.get(
            "write_enabled", True
        ):
            raise EvolvingMemoryAccessError("可演化记忆写入已禁用")

    def _normalize_scope(
        self,
        *,
        context: MemoryAccessContext,
        scope: MemoryScope | None,
        actor_type: MemoryActorType,
    ) -> MemoryScope:
        if scope is None:
            scope = MemoryScope(
                str(self.identity_config.get("default_scope", MemoryScope.PERSONA.value))
            )
        if actor_type == MemoryActorType.AUTOMATIC and context.is_group:
            scope = MemoryScope.SESSION
        if scope == MemoryScope.PUBLIC and actor_type == MemoryActorType.AUTOMATIC:
            raise EvolvingMemoryAccessError("自动动作禁止 public scope")
        if scope == MemoryScope.PUBLIC and actor_type != MemoryActorType.ADMIN:
            raise EvolvingMemoryAccessError("public scope 只能由管理员手工创建或修改")
        if scope == MemoryScope.LEGACY_SESSION and actor_type != MemoryActorType.MIGRATION:
            raise EvolvingMemoryAccessError("legacy_session 仅供内部迁移使用")
        return scope

    @staticmethod
    def canonicalize(content: str) -> str:
        return " ".join(content.casefold().split())

    @staticmethod
    def _scope_compatible(
        item: MemoryItem,
        target_scope: MemoryScope,
        context: MemoryAccessContext,
    ) -> bool:
        if item.scope != target_scope:
            return False
        if target_scope == MemoryScope.PERSONA:
            return item.persona_id == context.persona_id
        if target_scope in {MemoryScope.SESSION, MemoryScope.LEGACY_SESSION}:
            return item.session_id == context.session_id
        return True

    async def find_duplicate_candidates(
        self,
        *,
        context: MemoryAccessContext,
        content: str,
        canonical_key: str | None = None,
        limit: int = 10,
    ) -> list[DuplicateCandidate]:
        key = (canonical_key or self.canonicalize(content)).strip()
        return await self.store.find_duplicate_candidates(
            context=context,
            content=content,
            canonical_key=key,
            limit=limit,
        )

    async def create(
        self,
        *,
        context: MemoryAccessContext,
        content: str,
        operation_key: str,
        expected_version: int = 0,
        scope: MemoryScope | None = None,
        item_type: str = "fact",
        canonical_key: str | None = None,
        structured_payload: dict[str, Any] | None = None,
        importance: float = 0.5,
        confidence: float = 0.7,
        group_safe: bool = False,
        actor_type: MemoryActorType = MemoryActorType.AUTOMATIC,
        actor_id: str = "automatic-feedback",
        reason: str | None = None,
        source: dict[str, Any] | None = None,
        memory_item_id: str | None = None,
        revision_operation: RevisionOperation = RevisionOperation.CREATE,
    ) -> MutationResult:
        self._require_write_enabled()
        effective_scope = self._normalize_scope(
            context=context, scope=scope, actor_type=actor_type
        )
        normalized_content = content.strip()
        key = (canonical_key or self.canonicalize(normalized_content)).strip()
        if not normalized_content or not key:
            raise ValueError("content 和 canonical_key 不得为空")

        candidates = [
            candidate
            for candidate in await self.find_duplicate_candidates(
                context=context,
                content=normalized_content,
                canonical_key=key,
                limit=10,
            )
            if self._scope_compatible(candidate.item, effective_scope, context)
        ]
        exact = next((candidate for candidate in candidates if candidate.match_type == "exact"), None)
        if exact is not None and memory_item_id is None:
            item = exact.item
            if source:
                await self.store.add_source(
                    owner_user_id=context.owner_user_id,
                    memory_item_id=item.memory_item_id,
                    revision_no=item.current_revision_no,
                    source=source,
                )
            item, _ = await self.store.record_feedback(
                context=context,
                memory_item_id=item.memory_item_id,
                expected_version=item.version,
                useful=True,
                score_delta=0.05,
                operation_key=f"{operation_key}:exact-reinforce",
                actor_type=actor_type,
                actor_id=actor_id,
                reason="exact duplicate reinforcement",
            )
            result = MutationResult(
                action=MemoryAction.CREATE,
                item=item,
                affected_item_ids=(item.memory_item_id,),
                deduplicated=True,
                operation_key=operation_key,
                projection_status=item.index_status,
                duplicate_candidates=tuple(
                    candidate.item.memory_item_id for candidate in candidates
                ),
            )
            return await self._project(result)

        canonical = next(
            (candidate for candidate in candidates if candidate.match_type == "canonical"),
            None,
        )
        if canonical is not None and memory_item_id is None:
            item, deduplicated = await self.store.update_item(
                context=context,
                memory_item_id=canonical.item.memory_item_id,
                expected_version=canonical.item.version,
                operation_key=operation_key,
                actor_type=actor_type,
                actor_id=actor_id,
                content=normalized_content,
                canonical_key=key,
                structured_payload=structured_payload,
                item_type=item_type,
                importance=importance,
                confidence=confidence,
                group_safe=group_safe,
                reason=reason or "canonical duplicate update",
                source=source,
            )
            result = MutationResult(
                action=MemoryAction.UPDATE,
                item=item,
                affected_item_ids=(item.memory_item_id,),
                deduplicated=True,
                operation_key=operation_key,
                projection_status=item.index_status,
                duplicate_candidates=tuple(
                    candidate.item.memory_item_id for candidate in candidates
                ),
            )
            return await self._project(result)

        item, replayed = await self.store.create_item(
            context=context,
            scope=effective_scope,
            content=normalized_content,
            canonical_key=key,
            item_type=item_type,
            expected_version=expected_version,
            operation_key=operation_key,
            actor_type=actor_type,
            actor_id=actor_id,
            reason=reason,
            structured_payload=structured_payload,
            session_id=(
                context.session_id
                if effective_scope in {MemoryScope.SESSION, MemoryScope.LEGACY_SESSION}
                else None
            ),
            persona_id=(context.persona_id if effective_scope == MemoryScope.PERSONA else None),
            importance=float(importance),
            confidence=float(confidence),
            group_safe=bool(group_safe),
            memory_item_id=memory_item_id,
            revision_operation=revision_operation,
            source=source,
        )
        result = MutationResult(
            action=MemoryAction.CREATE,
            item=item,
            affected_item_ids=(item.memory_item_id,),
            deduplicated=replayed,
            operation_key=operation_key,
            projection_status=item.index_status,
            duplicate_candidates=tuple(candidate.item.memory_item_id for candidate in candidates),
        )
        return await self._project(result)

    async def update(
        self,
        *,
        context: MemoryAccessContext,
        memory_item_id: str,
        expected_version: int,
        operation_key: str,
        actor_type: MemoryActorType,
        actor_id: str,
        content: str | None = None,
        canonical_key: str | None = None,
        structured_payload: dict[str, Any] | None = None,
        item_type: str | None = None,
        importance: float | None = None,
        confidence: float | None = None,
        group_safe: bool | None = None,
        reason: str | None = None,
        source: dict[str, Any] | None = None,
    ) -> MutationResult:
        self._require_write_enabled()
        current = await self._require_item(context, memory_item_id)
        self._validate_existing_scope(current, actor_type)
        updated, replayed = await self.store.update_item(
            context=context,
            memory_item_id=memory_item_id,
            expected_version=expected_version,
            operation_key=operation_key,
            actor_type=actor_type,
            actor_id=actor_id,
            content=content,
            canonical_key=canonical_key,
            structured_payload=structured_payload,
            item_type=item_type,
            importance=importance,
            confidence=confidence,
            group_safe=group_safe,
            reason=reason,
            source=source,
        )
        return await self._project(
            MutationResult(
                action=MemoryAction.UPDATE,
                item=updated,
                affected_item_ids=(memory_item_id,),
                deduplicated=replayed,
                operation_key=operation_key,
                projection_status=updated.index_status,
            )
        )

    async def admin_update(
        self,
        *,
        context: MemoryAccessContext,
        target_context: MemoryAccessContext,
        memory_item_id: str,
        expected_version: int,
        operation_key: str,
        actor_id: str,
        content: str | None = None,
        canonical_key: str | None = None,
        structured_payload: dict[str, Any] | None = None,
        item_type: str | None = None,
        importance: float | None = None,
        confidence: float | None = None,
        group_safe: bool | None = None,
        status: MemoryItemStatus | None = None,
        scope: MemoryScope | None = None,
        session_id: str | None = None,
        persona_id: str | None = None,
        new_owner_user_id: str | None = None,
        reason: str | None = None,
        source: dict[str, Any] | None = None,
    ) -> MutationResult:
        self._require_write_enabled()
        current = await self._require_item(context, memory_item_id)
        self._validate_existing_scope(current, MemoryActorType.ADMIN)
        updated, replayed = await self.store.admin_update_item(
            context=context,
            target_context=target_context,
            memory_item_id=memory_item_id,
            expected_version=expected_version,
            operation_key=operation_key,
            actor_id=actor_id,
            content=content,
            canonical_key=canonical_key,
            structured_payload=structured_payload,
            item_type=item_type,
            importance=importance,
            confidence=confidence,
            group_safe=group_safe,
            status=status,
            scope=scope,
            session_id=session_id,
            persona_id=persona_id,
            new_owner_user_id=new_owner_user_id,
            reason=reason,
            source=source,
        )
        return await self._project(
            MutationResult(
                action=MemoryAction.UPDATE,
                item=updated,
                affected_item_ids=(memory_item_id,),
                deduplicated=replayed,
                operation_key=operation_key,
                projection_status=updated.index_status,
            )
        )

    async def merge(
        self,
        *,
        context: MemoryAccessContext,
        survivor_item_id: str,
        source_item_ids: list[str],
        expected_versions: dict[str, int],
        content: str,
        operation_key: str,
        actor_type: MemoryActorType,
        actor_id: str,
        canonical_key: str | None = None,
        structured_payload: dict[str, Any] | None = None,
        reason: str | None = None,
    ) -> MutationResult:
        self._require_write_enabled()
        for item_id in [survivor_item_id, *source_item_ids]:
            self._validate_existing_scope(await self._require_item(context, item_id), actor_type)
        survivor, replayed = await self.store.merge_items(
            context=context,
            survivor_item_id=survivor_item_id,
            source_item_ids=source_item_ids,
            expected_versions=expected_versions,
            content=content,
            canonical_key=canonical_key or self.canonicalize(content),
            operation_key=operation_key,
            actor_type=actor_type,
            actor_id=actor_id,
            reason=reason,
            structured_payload=structured_payload,
        )
        affected = tuple(dict.fromkeys([survivor_item_id, *source_item_ids]))
        return await self._project(
            MutationResult(
                action=MemoryAction.MERGE,
                item=survivor,
                affected_item_ids=affected,
                deduplicated=replayed,
                operation_key=operation_key,
                projection_status=survivor.index_status,
            )
        )

    async def supersede(
        self,
        *,
        context: MemoryAccessContext,
        old_item_id: str,
        replacement_item_id: str,
        expected_versions: dict[str, int],
        operation_key: str,
        actor_type: MemoryActorType,
        actor_id: str,
        reason: str | None = None,
    ) -> MutationResult:
        self._require_write_enabled()
        for item_id in (old_item_id, replacement_item_id):
            self._validate_existing_scope(await self._require_item(context, item_id), actor_type)
        replacement, replayed = await self.store.supersede_item(
            context=context,
            old_item_id=old_item_id,
            replacement_item_id=replacement_item_id,
            expected_versions=expected_versions,
            operation_key=operation_key,
            actor_type=actor_type,
            actor_id=actor_id,
            reason=reason,
        )
        return await self._project(
            MutationResult(
                action=MemoryAction.SUPERSEDE,
                item=replacement,
                affected_item_ids=(replacement_item_id, old_item_id),
                deduplicated=replayed,
                operation_key=operation_key,
                projection_status=replacement.index_status,
            )
        )

    async def archive(
        self,
        *,
        context: MemoryAccessContext,
        memory_item_id: str,
        expected_version: int,
        operation_key: str,
        actor_type: MemoryActorType,
        actor_id: str,
        reason: str | None = None,
    ) -> MutationResult:
        self._require_write_enabled()
        self._validate_existing_scope(await self._require_item(context, memory_item_id), actor_type)
        archived, replayed = await self.store.archive_item(
            context=context,
            memory_item_id=memory_item_id,
            expected_version=expected_version,
            operation_key=operation_key,
            actor_type=actor_type,
            actor_id=actor_id,
            reason=reason,
        )
        return await self._project(
            MutationResult(
                action=MemoryAction.ARCHIVE,
                item=archived,
                affected_item_ids=(memory_item_id,),
                deduplicated=replayed,
                operation_key=operation_key,
                projection_status=archived.index_status,
            )
        )

    async def useful_feedback(
        self,
        *,
        context: MemoryAccessContext,
        feedback: MemoryFeedback,
    ) -> MemoryItem:
        if not self.evolving_config.get("feedback_enabled", True):
            raise EvolvingMemoryAccessError("记忆反馈已禁用")
        current = await self._require_item(context, feedback.memory_item_id)
        self._validate_existing_scope(current, feedback.actor_type)
        item, _replayed = await self.store.record_feedback(
            context=context,
            memory_item_id=feedback.memory_item_id,
            expected_version=feedback.expected_version,
            useful=feedback.useful,
            score_delta=feedback.score_delta,
            operation_key=feedback.operation_key,
            actor_type=feedback.actor_type,
            actor_id=feedback.actor_id,
            reason=feedback.reason,
        )
        return item

    async def _require_item(
        self, context: MemoryAccessContext, memory_item_id: str
    ) -> MemoryItem:
        item = await self.store.get_item(
            owner_user_id=context.owner_user_id,
            memory_item_id=memory_item_id,
            context=context,
        )
        if item is None:
            raise EvolvingMemoryNotFoundError()
        return item

    @staticmethod
    def _validate_existing_scope(item: MemoryItem, actor_type: MemoryActorType) -> None:
        if item.scope == MemoryScope.PUBLIC and actor_type != MemoryActorType.ADMIN:
            raise EvolvingMemoryAccessError("自动或普通用户动作禁止修改 public 记忆")
        if item.scope == MemoryScope.LEGACY_SESSION and actor_type != MemoryActorType.MIGRATION:
            raise EvolvingMemoryAccessError("legacy_session 仅允许迁移流程修改")

    async def _project(self, result: MutationResult) -> MutationResult:
        if self.projection_callback is None:
            return result
        try:
            callback_result = self.projection_callback(result)
            if inspect.isawaitable(callback_result):
                callback_result = await callback_result
            document_id = None
            if isinstance(callback_result, dict):
                raw_document_id = callback_result.get("current_document_id")
                if raw_document_id is not None:
                    document_id = int(raw_document_id)
            await self.store.set_index_status(
                owner_user_id=result.item.owner_user_id,
                memory_item_id=result.item.memory_item_id,
                status=IndexStatus.CURRENT,
                current_document_id=document_id,
            )
            item = await self.store.get_item(
                owner_user_id=result.item.owner_user_id,
                memory_item_id=result.item.memory_item_id,
            )
            return result.model_copy(
                update={
                    "item": item or result.item,
                    "projection_status": IndexStatus.CURRENT,
                }
            )
        except Exception as exc:
            await self.store.set_index_status(
                owner_user_id=result.item.owner_user_id,
                memory_item_id=result.item.memory_item_id,
                status=IndexStatus.NEEDS_REPAIR,
                error=str(exc),
            )
            item = await self.store.get_item(
                owner_user_id=result.item.owner_user_id,
                memory_item_id=result.item.memory_item_id,
            )
            return result.model_copy(
                update={
                    "item": item or result.item,
                    "projection_status": IndexStatus.NEEDS_REPAIR,
                }
            )

    async def backfill_legacy_key_facts(
        self,
        *,
        batch_size: int | None = None,
        max_batches: int | None = None,
    ) -> dict[str, int]:
        """Deterministically backfill legacy documents.key_facts with checkpoints."""
        effective_batch = max(
            1,
            min(
                int(batch_size or self.evolving_config.get("migration_batch_size", 100)),
                1000,
            ),
        )
        checkpoint = await self.store.get_migration_checkpoint(_BACKFILL_CHECKPOINT)
        cursor_id = int(checkpoint.get("cursor", 0) or 0)
        totals = {
            "created": int(checkpoint.get("created", 0) or 0),
            "dedup": int(checkpoint.get("dedup", 0) or 0),
            "skipped": int(checkpoint.get("skipped", 0) or 0),
            "conflicted": int(checkpoint.get("conflicted", 0) or 0),
            "errors": int(checkpoint.get("errors", 0) or 0),
        }
        processed_batches = 0

        while max_batches is None or processed_batches < max_batches:
            documents = await self._read_legacy_documents(cursor_id, effective_batch)
            if not documents:
                await self.store.set_migration_checkpoint(
                    _BACKFILL_CHECKPOINT,
                    {**totals, "cursor": cursor_id, "status": "complete"},
                )
                break

            candidates: list[dict[str, Any]] = []
            batch_skipped = 0
            for document in documents:
                document_id = int(document["id"])
                metadata = self._parse_metadata(document.get("metadata"))
                facts = metadata.get("key_facts")
                if not isinstance(facts, list):
                    batch_skipped += 1
                    cursor_id = max(cursor_id, document_id)
                    continue
                for fact_index, raw_fact in enumerate(facts):
                    fact = str(raw_fact).strip()
                    if not fact:
                        batch_skipped += 1
                        continue
                    candidate = await self._build_backfill_candidate(
                        document_id=document_id,
                        fact_index=fact_index,
                        fact=fact,
                        document_text=str(document.get("text") or ""),
                        metadata=metadata,
                    )
                    candidates.append(candidate)
                cursor_id = max(cursor_id, document_id)

            batch_checkpoint = {
                **totals,
                "cursor": cursor_id,
                "status": "running",
                "skipped": totals["skipped"] + batch_skipped,
            }
            batch_stats = await self.store.backfill_batch(
                candidates=candidates,
                checkpoint_key=_BACKFILL_CHECKPOINT,
                checkpoint=batch_checkpoint,
            )
            totals["created"] += batch_stats["created"]
            totals["dedup"] += batch_stats["dedup"]
            totals["conflicted"] += batch_stats["conflicted"]
            totals["errors"] += batch_stats["errors"]
            totals["skipped"] += batch_skipped
            processed_batches += 1

        return {**totals, "cursor": cursor_id}

    async def _read_legacy_documents(
        self, after_id: int, limit: int
    ) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.store.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA query_only = ON")
            cursor = await db.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'documents'"
            )
            if await cursor.fetchone() is None:
                return []
            cursor = await db.execute("PRAGMA table_info(documents)")
            columns = {str(row[1]) for row in await cursor.fetchall()}
            if "id" not in columns:
                return []
            text_expression = "text" if "text" in columns else "'' AS text"
            metadata_expression = "metadata" if "metadata" in columns else "'{}' AS metadata"
            cursor = await db.execute(
                f"""
                SELECT id, {text_expression}, {metadata_expression}
                FROM documents
                WHERE id > ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (after_id, limit),
            )
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def _build_backfill_candidate(
        self,
        *,
        document_id: int,
        fact_index: int,
        fact: str,
        document_text: str,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        normalized = self.canonicalize(fact)
        normalized_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        deterministic = uuid.uuid5(
            _BACKFILL_NAMESPACE,
            f"{document_id}:{fact_index}:{normalized_hash}",
        )
        item_id = f"mem_{deterministic.hex}"
        session_id = str(metadata.get("session_id") or f"legacy-document:{document_id}")
        persona_id = self._optional_text(metadata.get("persona_id"))
        is_group = bool(
            metadata.get("is_group")
            or metadata.get("group_id")
            or ":GroupMessage:" in session_id
            or ":group:" in session_id.casefold()
        )
        platform_id = self._optional_text(
            metadata.get("platform_id") or metadata.get("platform")
        )
        bot_id = self._optional_text(metadata.get("bot_id"))
        external_user_id = self._explicit_external_user_id(metadata)
        owner_id = self._optional_text(metadata.get("owner_user_id"))

        if owner_id:
            resolved_owner = owner_id
        elif platform_id and bot_id and external_user_id:
            link = await self.store.resolve_identity(
                platform_id=platform_id,
                bot_id=bot_id,
                external_user_id=external_user_id,
                create_if_missing=True,
            )
            resolved_owner = link.owner_user_id if link else None
        else:
            resolved_owner = None

        source_window = metadata.get("source_window")
        owner_is_in_source = self._source_window_contains_user(
            source_window, external_user_id
        )
        if is_group:
            if resolved_owner and external_user_id and owner_is_in_source:
                scope = MemoryScope.SESSION
                owner_for_item = resolved_owner
            else:
                scope = MemoryScope.LEGACY_SESSION
                owner_for_item = f"owner_legacy_{uuid.uuid5(_BACKFILL_NAMESPACE, session_id).hex}"
        elif resolved_owner:
            scope = MemoryScope.PERSONA if persona_id else MemoryScope.USER
            owner_for_item = resolved_owner
        else:
            scope = MemoryScope.LEGACY_SESSION
            owner_for_item = f"owner_legacy_{uuid.uuid5(_BACKFILL_NAMESPACE, session_id).hex}"

        source = self._legacy_source(
            document_id=document_id,
            fact_index=fact_index,
            normalized_hash=normalized_hash,
            fact=fact,
            document_text=document_text,
            session_id=session_id,
            source_window=source_window,
        )
        importance = self._bounded_float(metadata.get("importance"), 0.5)
        confidence = self._bounded_float(metadata.get("confidence"), 0.7)
        return {
            "memory_item_id": item_id,
            "owner_user_id": owner_for_item,
            "owner_metadata": {
                "legacy_isolated": scope == MemoryScope.LEGACY_SESSION,
                "source_session_id": session_id,
            },
            "scope": scope.value,
            "session_id": (
                session_id
                if scope in {MemoryScope.SESSION, MemoryScope.LEGACY_SESSION}
                else None
            ),
            "persona_id": persona_id if scope == MemoryScope.PERSONA else None,
            "item_type": "fact",
            "canonical_key": normalized[:2048],
            "content": fact[:65536],
            "structured_payload": {
                "legacy_document_id": document_id,
                "legacy_fact_index": fact_index,
            },
            "importance": importance,
            "confidence": confidence,
            "group_safe": False,
            "source": source,
            "operation_key": f"backfill:{item_id}",
        }

    @staticmethod
    def _parse_metadata(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        if not value:
            return {}
        try:
            loaded = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return {}
        return loaded if isinstance(loaded, dict) else {}

    @staticmethod
    def _optional_text(value: Any) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip()
        return normalized or None

    @classmethod
    def _explicit_external_user_id(cls, metadata: dict[str, Any]) -> str | None:
        direct = cls._optional_text(
            metadata.get("external_user_id")
            or metadata.get("creator_sender_id")
            or metadata.get("sender_id")
        )
        if direct:
            return direct
        source_window = metadata.get("source_window")
        if isinstance(source_window, dict):
            return cls._optional_text(
                source_window.get("external_user_id")
                or source_window.get("sender_id")
            )
        return None

    @staticmethod
    def _source_window_contains_user(source_window: Any, user_id: str | None) -> bool:
        if not user_id or not isinstance(source_window, dict):
            return False
        values: list[str] = []
        for key in ("sender_id", "external_user_id"):
            if source_window.get(key) is not None:
                values.append(str(source_window[key]))
        for key in ("sender_ids", "participants"):
            raw = source_window.get(key)
            if isinstance(raw, list):
                values.extend(str(value) for value in raw)
        return user_id in values

    @staticmethod
    def _legacy_source(
        *,
        document_id: int,
        fact_index: int,
        normalized_hash: str,
        fact: str,
        document_text: str,
        session_id: str,
        source_window: Any,
    ) -> dict[str, Any]:
        start_id = None
        end_id = None
        availability = SourceAvailability.UNAVAILABLE
        metadata: dict[str, Any] = {"legacy_fact_index": fact_index}
        if isinstance(source_window, dict):
            for key in ("message_start_id", "start_message_id", "start_id"):
                if source_window.get(key) is not None:
                    try:
                        start_id = int(source_window[key])
                    except (TypeError, ValueError):
                        start_id = None
                    break
            for key in ("message_end_id", "end_message_id", "end_id"):
                if source_window.get(key) is not None:
                    try:
                        end_id = int(source_window[key])
                    except (TypeError, ValueError):
                        end_id = None
                    break
            availability = (
                SourceAvailability.AVAILABLE
                if start_id is not None and end_id is not None
                else SourceAvailability.PARTIAL
            )
            metadata["source_window"] = source_window
        snapshot = fact if not document_text else f"{fact}\n\n{document_text[:4096]}"
        return {
            "source_key": f"legacy-document:{document_id}:{fact_index}:{normalized_hash}",
            "source_type": "legacy_document_key_fact",
            "source_ref": f"document:{document_id}",
            "document_id": document_id,
            "session_id": session_id,
            "message_start_id": start_id,
            "message_end_id": end_id,
            "content_snapshot": snapshot,
            "availability": availability.value,
            "metadata": metadata,
        }

    @staticmethod
    def _bounded_float(value: Any, default: float) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return float(default)
        return max(0.0, min(1.0, number))


__all__ = ["EvolvingMemoryManager", "ProjectionCallback"]
