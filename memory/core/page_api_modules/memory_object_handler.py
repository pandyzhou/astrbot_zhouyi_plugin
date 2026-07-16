"""Owner-scoped Page API for revisioned memory objects and conflicts."""

from __future__ import annotations

from typing import Any

from ..base.exceptions import EvolvingMemoryNotFoundError, EvolvingMemoryVersionConflictError
from ..models.evolving_memory import (
    ConflictStatus,
    IndexStatus,
    MemoryActorType,
    MemoryConflict,
    MemoryItem,
    MemoryItemStatus,
    MemoryScope,
)
from .schemas import (
    memory_object_payload,
    relation_payload,
    revision_payload,
    source_payload,
)
from .utils import PageApiProblem, PageApiUtils


class MemoryObjectHandler:
    def __init__(self, utils: PageApiUtils, memory_service: Any) -> None:
        self.utils = utils
        self.memory_service = memory_service

    def _components(self):
        return self.utils.resolve_evolving_components(self.memory_service)

    @staticmethod
    def _scope(value: Any, *, default: MemoryScope | None = None) -> MemoryScope | None:
        if value is None or value == "":
            return default
        try:
            return MemoryScope(str(value).strip())
        except ValueError as exc:
            raise PageApiProblem(
                "scope 无效",
                status=422,
                code="MEMORY_SCOPE_INVALID",
            ) from exc

    @staticmethod
    def _status(value: Any) -> MemoryItemStatus | None:
        if value is None or value == "":
            return None
        try:
            return MemoryItemStatus(str(value).strip())
        except ValueError as exc:
            raise PageApiProblem(
                "status 无效",
                status=422,
                code="MEMORY_STATUS_INVALID",
            ) from exc

    @staticmethod
    def _index_status(value: Any) -> IndexStatus | None:
        if value is None or value == "":
            return None
        normalized = str(value).strip()
        if normalized == "synced":
            normalized = IndexStatus.CURRENT.value
        try:
            return IndexStatus(normalized)
        except ValueError as exc:
            raise PageApiProblem(
                "index_status 无效",
                status=422,
                code="MEMORY_INDEX_STATUS_INVALID",
            ) from exc

    @staticmethod
    def _bounded_float(payload: dict[str, Any], name: str, default: float | None = None) -> float | None:
        if name not in payload:
            return default
        value = payload.get(name)
        if isinstance(value, bool):
            raise PageApiProblem(
                f"{name} 必须是 0-1 数字",
                status=422,
                code="MEMORY_VALIDATION_FAILED",
            )
        try:
            parsed = float(value)
        except (TypeError, ValueError) as exc:
            raise PageApiProblem(
                f"{name} 必须是 0-1 数字",
                status=422,
                code="MEMORY_VALIDATION_FAILED",
            ) from exc
        if not 0.0 <= parsed <= 1.0:
            raise PageApiProblem(
                f"{name} 必须在 0-1 范围内",
                status=422,
                code="MEMORY_VALIDATION_FAILED",
            )
        return parsed

    @staticmethod
    def _optional_bool(payload: dict[str, Any], name: str, default: bool | None = None) -> bool | None:
        if name not in payload:
            return default
        value = payload.get(name)
        if not isinstance(value, bool):
            raise PageApiProblem(
                f"{name} 必须是布尔值",
                status=422,
                code="MEMORY_VALIDATION_FAILED",
            )
        return value

    @staticmethod
    def _structured_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
        if "structured_payload" not in payload:
            return None
        value = payload.get("structured_payload")
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise PageApiProblem(
                "structured_payload 必须是对象或 null",
                status=422,
                code="MEMORY_VALIDATION_FAILED",
            )
        return dict(value)

    async def _item(self, manager: Any, store: Any, owner_user_id: str, memory_item_id: str) -> tuple[MemoryItem, Any]:
        item = await store.get_item(
            owner_user_id=owner_user_id,
            memory_item_id=memory_item_id,
        )
        if item is None:
            raise EvolvingMemoryNotFoundError()
        context = await manager.build_admin_access_context(
            owner_user_id=owner_user_id,
            session_id=item.session_id,
            persona_id=item.persona_id,
        )
        checked = await store.get_item(
            owner_user_id=owner_user_id,
            memory_item_id=memory_item_id,
            context=context,
        )
        if checked is None:
            raise EvolvingMemoryNotFoundError()
        return checked, context

    async def _conflict_payload(
        self,
        manager: Any,
        store: Any,
        conflict: MemoryConflict,
    ) -> dict[str, Any]:
        left = await store.get_item(
            owner_user_id=conflict.owner_user_id,
            memory_item_id=conflict.left_item_id,
        )
        right = await store.get_item(
            owner_user_id=conflict.owner_user_id,
            memory_item_id=conflict.right_item_id,
        )
        if left is None or right is None:
            raise EvolvingMemoryNotFoundError("冲突引用的记忆对象不存在")
        owner = await store.get_owner(conflict.owner_user_id)
        counts = await store.get_item_admin_counts(
            owner_user_id=conflict.owner_user_id,
            memory_item_ids=(left.memory_item_id, right.memory_item_id),
        )
        display_name = owner.display_name if owner else None
        return {
            "conflict_id": conflict.conflict_id,
            "owner_user_id": conflict.owner_user_id,
            "conflict_type": conflict.conflict_type,
            "severity": conflict.severity.value,
            "status": conflict.status.value,
            "left_item": memory_object_payload(
                left,
                owner_display_name=display_name,
                counts=counts.get(left.memory_item_id),
            ),
            "right_item": memory_object_payload(
                right,
                owner_display_name=display_name,
                counts=counts.get(right.memory_item_id),
            ),
            "resolution": conflict.resolution_action,
            "resolution_reason": conflict.resolution_note,
            "resolved_by": conflict.resolved_by,
            "metadata": dict(conflict.metadata),
            "created_at": self.utils.unix_timestamp(conflict.created_at),
            "updated_at": self.utils.unix_timestamp(conflict.updated_at),
            "resolved_at": self.utils.unix_timestamp(conflict.resolved_at),
        }

    async def _detail_payload(self, manager: Any, store: Any, item: MemoryItem) -> dict[str, Any]:
        owner = await store.get_owner(item.owner_user_id)
        counts = await store.get_item_admin_counts(
            owner_user_id=item.owner_user_id,
            memory_item_ids=(item.memory_item_id,),
        )
        relations = await store.list_relations(
            owner_user_id=item.owner_user_id,
            memory_item_id=item.memory_item_id,
        )
        relation_payloads: list[dict[str, Any]] = []
        for relation in relations:
            other_id = (
                relation.target_item_id
                if relation.source_item_id == item.memory_item_id
                else relation.source_item_id
            )
            other = await store.get_item(
                owner_user_id=item.owner_user_id,
                memory_item_id=other_id,
            )
            relation_payloads.append(
                relation_payload(
                    relation,
                    target_content=other.content if other is not None else None,
                )
            )
        all_conflicts = await store.list_conflicts(
            owner_user_id=item.owner_user_id,
            limit=500,
        )
        conflicts = [
            await self._conflict_payload(manager, store, conflict)
            for conflict in all_conflicts
            if item.memory_item_id in {conflict.left_item_id, conflict.right_item_id}
        ]
        return {
            "item": memory_object_payload(
                item,
                owner_display_name=owner.display_name if owner else None,
                counts=counts.get(item.memory_item_id),
            ),
            "relations": relation_payloads,
            "conflicts": conflicts,
        }

    @PageApiUtils.guarded
    async def list_objects(self):
        self.utils.require_actor_username()
        manager, store = self._components()
        owner_user_id = self.utils.required_text(
            {"owner_user_id": self.utils.query_value("owner_user_id")},
            "owner_user_id",
        )
        await manager.build_admin_access_context(owner_user_id=owner_user_id)
        try:
            page = max(1, int(self.utils.query_value("page", 1)))
            page_size = min(500, max(1, int(self.utils.query_value("page_size", 20))))
        except (TypeError, ValueError) as exc:
            raise PageApiProblem(
                "分页参数无效",
                status=400,
                code="MEMORY_INVALID_REQUEST",
            ) from exc
        conflict_value = self.utils.optional_text(self.utils.query_value("conflict"))
        conflict = None
        if conflict_value is not None:
            if conflict_value not in {"yes", "no"}:
                raise PageApiProblem(
                    "conflict 必须是 yes 或 no",
                    status=422,
                    code="MEMORY_VALIDATION_FAILED",
                )
            conflict = conflict_value == "yes"
        sort_value = str(self.utils.query_value("sort", "updated_desc") or "updated_desc")
        sort_options = {
            "updated_desc": ("updated_at", "desc"),
            "updated_asc": ("updated_at", "asc"),
            "created_desc": ("created_at", "desc"),
            "created_asc": ("created_at", "asc"),
            "importance_desc": ("importance", "desc"),
            "importance_asc": ("importance", "asc"),
            "confidence_desc": ("confidence", "desc"),
            "confidence_asc": ("confidence", "asc"),
            "version_desc": ("version", "desc"),
            "version_asc": ("version", "asc"),
        }
        if sort_value not in sort_options:
            raise PageApiProblem(
                "sort 无效",
                status=422,
                code="MEMORY_VALIDATION_FAILED",
            )
        sort_by, direction = sort_options[sort_value]
        items, total = await store.list_items_for_owner(
            owner_user_id=owner_user_id,
            keyword=self.utils.optional_text(self.utils.query_value("keyword")),
            scope=self._scope(self.utils.query_value("scope")),
            persona_id=self.utils.optional_text(self.utils.query_value("persona_id")),
            status=self._status(self.utils.query_value("status")),
            item_type=self.utils.optional_text(
                self.utils.query_value("memory_type") or self.utils.query_value("item_type")
            ),
            conflict=conflict,
            index_status=self._index_status(self.utils.query_value("index_status")),
            sort_by=sort_by,
            sort_direction=direction,
            limit=page_size,
            offset=(page - 1) * page_size,
        )
        owner = await store.get_owner(owner_user_id)
        counts = await store.get_item_admin_counts(
            owner_user_id=owner_user_id,
            memory_item_ids=(item.memory_item_id for item in items),
        )
        return self.utils.ok(
            {
                "items": [
                    memory_object_payload(
                        item,
                        owner_display_name=owner.display_name if owner else None,
                        counts=counts.get(item.memory_item_id),
                    )
                    for item in items
                ],
                "total": total,
                "page": page,
                "page_size": page_size,
                "has_more": page * page_size < total,
            }
        )

    @PageApiUtils.guarded
    async def get_object_detail(self):
        self.utils.require_actor_username()
        manager, store = self._components()
        owner_user_id = self.utils.required_text(
            {"owner_user_id": self.utils.query_value("owner_user_id")},
            "owner_user_id",
        )
        memory_item_id = self.utils.required_text(
            {"memory_item_id": self.utils.query_value("memory_item_id")},
            "memory_item_id",
        )
        item, _context = await self._item(manager, store, owner_user_id, memory_item_id)
        return self.utils.ok(await self._detail_payload(manager, store, item))

    @PageApiUtils.guarded
    async def create_object(self):
        manager, store = self._components()
        actor = self.utils.require_actor_username()
        payload = await self.utils.request_payload()
        owner_user_id = self.utils.required_text(payload, "owner_user_id")
        content = self.utils.required_text(payload, "content")
        scope = self._scope(payload.get("scope"), default=MemoryScope.PERSONA)
        assert scope is not None
        expected_version = self.utils.required_int(payload, "expected_version", minimum=0)
        if expected_version != 0:
            raise EvolvingMemoryVersionConflictError("new", expected_version, 0)
        persona_id = self.utils.optional_text(payload.get("persona_id"))
        session_id = self.utils.optional_text(payload.get("session_id"))
        context = await manager.build_admin_access_context(
            owner_user_id=owner_user_id,
            session_id=session_id,
            persona_id=persona_id,
        )
        item_type = self.utils.optional_text(payload.get("item_type") or payload.get("memory_type")) or "fact"
        source = payload.get("source")
        if source is not None and not isinstance(source, dict):
            raise PageApiProblem(
                "source 必须是对象",
                status=422,
                code="MEMORY_VALIDATION_FAILED",
            )
        result = await manager.create(
            context=context,
            content=content,
            operation_key=self.utils.operation_key("object-create", actor),
            expected_version=0,
            scope=scope,
            item_type=item_type,
            canonical_key=self.utils.optional_text(payload.get("canonical_key")),
            structured_payload=self._structured_payload(payload) or {},
            importance=self._bounded_float(payload, "importance", 0.5) or 0.0,
            confidence=self._bounded_float(payload, "confidence", 0.7) or 0.0,
            group_safe=self._optional_bool(payload, "group_safe", False) or False,
            actor_type=MemoryActorType.ADMIN,
            actor_id=actor,
            reason=self.utils.optional_text(payload.get("reason")),
            source=dict(source) if isinstance(source, dict) else None,
        )
        return self.utils.ok(await self._detail_payload(manager, store, result.item))

    @PageApiUtils.guarded
    async def update_object(self):
        manager, store = self._components()
        actor = self.utils.require_actor_username()
        payload = await self.utils.request_payload()
        owner_user_id = self.utils.required_text(payload, "owner_user_id")
        memory_item_id = self.utils.required_text(payload, "memory_item_id")
        expected_version = self.utils.required_int(payload, "expected_version", minimum=1)
        current, context = await self._item(manager, store, owner_user_id, memory_item_id)
        target_owner_user_id = self.utils.optional_text(payload.get("target_owner_user_id"))
        if target_owner_user_id is not None and target_owner_user_id != owner_user_id:
            raise PageApiProblem(
                "对象更新禁止跨 owner 移动；请使用 owner merge",
                status=422,
                code="MEMORY_ACCESS_CONTEXT_INVALID",
            )
        scope = self._scope(payload.get("scope"), default=current.scope)
        assert scope is not None
        persona_id = (
            self.utils.optional_text(payload.get("persona_id"))
            if "persona_id" in payload
            else current.persona_id
        )
        session_id = (
            self.utils.optional_text(payload.get("session_id"))
            if "session_id" in payload
            else current.session_id
        )
        target_context = await manager.build_admin_access_context(
            owner_user_id=owner_user_id,
            session_id=session_id,
            persona_id=persona_id,
        )
        result = await manager.admin_update(
            context=context,
            target_context=target_context,
            memory_item_id=memory_item_id,
            expected_version=expected_version,
            operation_key=self.utils.operation_key("object-update", actor),
            actor_id=actor,
            content=(self.utils.required_text(payload, "content") if "content" in payload else None),
            canonical_key=(
                self.utils.required_text(payload, "canonical_key")
                if "canonical_key" in payload
                else None
            ),
            structured_payload=self._structured_payload(payload),
            item_type=(
                self.utils.required_text(
                    {"item_type": payload.get("item_type") or payload.get("memory_type")},
                    "item_type",
                )
                if "item_type" in payload or "memory_type" in payload
                else None
            ),
            importance=self._bounded_float(payload, "importance"),
            confidence=self._bounded_float(payload, "confidence"),
            group_safe=self._optional_bool(payload, "group_safe"),
            status=self._status(payload.get("status")),
            scope=scope,
            session_id=session_id,
            persona_id=persona_id,
            new_owner_user_id=None,
            reason=self.utils.optional_text(payload.get("reason")),
        )
        return self.utils.ok(await self._detail_payload(manager, store, result.item))

    @PageApiUtils.guarded
    async def list_revisions(self):
        self.utils.require_actor_username()
        manager, store = self._components()
        owner_user_id = self.utils.required_text(
            {"owner_user_id": self.utils.query_value("owner_user_id")},
            "owner_user_id",
        )
        memory_item_id = self.utils.required_text(
            {"memory_item_id": self.utils.query_value("memory_item_id")},
            "memory_item_id",
        )
        await self._item(manager, store, owner_user_id, memory_item_id)
        revisions = await store.list_revisions(
            owner_user_id=owner_user_id,
            memory_item_id=memory_item_id,
        )
        return self.utils.ok({"revisions": [revision_payload(item) for item in revisions]})

    @PageApiUtils.guarded
    async def list_sources(self):
        self.utils.require_actor_username()
        manager, store = self._components()
        owner_user_id = self.utils.required_text(
            {"owner_user_id": self.utils.query_value("owner_user_id")},
            "owner_user_id",
        )
        memory_item_id = self.utils.required_text(
            {"memory_item_id": self.utils.query_value("memory_item_id")},
            "memory_item_id",
        )
        await self._item(manager, store, owner_user_id, memory_item_id)
        revision_no_raw = self.utils.query_value("revision_no")
        revision_no = int(revision_no_raw) if revision_no_raw not in {None, ""} else None
        sources = await store.list_sources(
            owner_user_id=owner_user_id,
            memory_item_id=memory_item_id,
        )
        if revision_no is not None:
            sources = [source for source in sources if source.revision_no == revision_no]
        return self.utils.ok({"sources": [source_payload(item) for item in sources]})

    async def _merge_selection(self, manager: Any, store: Any, payload: dict[str, Any]):
        owner_user_id = self.utils.required_text(payload, "owner_user_id")
        survivor_id = self.utils.required_text(payload, "survivor_memory_item_id")
        raw_sources = payload.get("source_memory_item_ids")
        if not isinstance(raw_sources, list):
            raise PageApiProblem(
                "source_memory_item_ids 必须是数组",
                status=400,
                code="MEMORY_INVALID_REQUEST",
            )
        source_ids = [self.utils.optional_text(value) for value in raw_sources]
        source_ids = list(dict.fromkeys(value for value in source_ids if value and value != survivor_id))
        if not source_ids:
            raise PageApiProblem(
                "merge 至少需要一个来源对象",
                status=422,
                code="MEMORY_VALIDATION_FAILED",
            )
        expected_versions = self.utils.expected_versions(payload.get("expected_versions"))
        survivor, context = await self._item(manager, store, owner_user_id, survivor_id)
        selected = [survivor]
        for item_id in source_ids:
            item = await store.get_item(
                owner_user_id=owner_user_id,
                memory_item_id=item_id,
                context=context,
            )
            if item is None:
                raise EvolvingMemoryNotFoundError()
            selected.append(item)
        for item in selected:
            expected = expected_versions.get(item.memory_item_id)
            if expected is None:
                raise PageApiProblem(
                    f"缺少 expected_version: {item.memory_item_id}",
                    status=400,
                    code="MEMORY_INVALID_REQUEST",
                )
            if expected != item.version:
                raise EvolvingMemoryVersionConflictError(item.memory_item_id, expected, item.version)
        return owner_user_id, survivor, source_ids, expected_versions, selected, context

    @PageApiUtils.guarded
    async def preview_merge(self):
        manager, store = self._components()
        self.utils.require_actor_username()
        payload = await self.utils.request_payload()
        _owner, survivor, source_ids, expected_versions, selected, _context = await self._merge_selection(
            manager, store, payload
        )
        merged_payload: dict[str, Any] = dict(survivor.structured_payload)
        warnings: list[str] = []
        for item in selected[1:]:
            for key, value in item.structured_payload.items():
                if key in merged_payload and merged_payload[key] != value:
                    warnings.append(f"structured_payload.{key} 存在不同值")
                    continue
                merged_payload[key] = value
        if len({item.scope for item in selected}) > 1:
            warnings.append("所选对象 scope 不同；执行时将保留 survivor scope")
        return self.utils.ok(
            {
                "owner_user_id": survivor.owner_user_id,
                "survivor_memory_item_id": survivor.memory_item_id,
                "source_memory_item_ids": source_ids,
                "merged_content": "\n".join(item.content for item in selected),
                "merged_structured_payload": merged_payload,
                "warnings": warnings,
                "expected_versions": expected_versions,
            }
        )

    @PageApiUtils.guarded
    async def merge_objects(self):
        manager, store = self._components()
        actor = self.utils.require_actor_username()
        payload = await self.utils.request_payload()
        _owner, survivor, source_ids, expected_versions, _selected, context = await self._merge_selection(
            manager, store, payload
        )
        content = self.utils.required_text(payload, "content")
        result = await manager.merge(
            context=context,
            survivor_item_id=survivor.memory_item_id,
            source_item_ids=source_ids,
            expected_versions=expected_versions,
            content=content,
            operation_key=self.utils.operation_key("object-merge", actor),
            actor_type=MemoryActorType.ADMIN,
            actor_id=actor,
            canonical_key=self.utils.optional_text(payload.get("canonical_key")),
            structured_payload=self._structured_payload(payload),
            reason=self.utils.optional_text(payload.get("reason")),
        )
        return self.utils.ok(await self._detail_payload(manager, store, result.item))

    @PageApiUtils.guarded
    async def supersede_object(self):
        manager, store = self._components()
        actor = self.utils.require_actor_username()
        payload = await self.utils.request_payload()
        owner_user_id = self.utils.required_text(payload, "owner_user_id")
        old_item_id = self.utils.required_text(
            {"old_memory_item_id": payload.get("old_memory_item_id") or payload.get("old_item_id")},
            "old_memory_item_id",
        )
        replacement_item_id = self.utils.required_text(
            {"new_memory_item_id": payload.get("new_memory_item_id") or payload.get("replacement_item_id")},
            "new_memory_item_id",
        )
        expected_versions = self.utils.expected_versions(payload.get("expected_versions"))
        old_item, context = await self._item(manager, store, owner_user_id, old_item_id)
        replacement = await store.get_item(
            owner_user_id=owner_user_id,
            memory_item_id=replacement_item_id,
            context=context,
        )
        if replacement is None:
            raise EvolvingMemoryNotFoundError()
        for item in (old_item, replacement):
            expected = expected_versions.get(item.memory_item_id)
            if expected is None:
                raise PageApiProblem(
                    f"缺少 expected_version: {item.memory_item_id}",
                    status=400,
                    code="MEMORY_INVALID_REQUEST",
                )
        result = await manager.supersede(
            context=context,
            old_item_id=old_item_id,
            replacement_item_id=replacement_item_id,
            expected_versions=expected_versions,
            operation_key=self.utils.operation_key("object-supersede", actor),
            actor_type=MemoryActorType.ADMIN,
            actor_id=actor,
            reason=self.utils.optional_text(payload.get("reason")),
        )
        return self.utils.ok(await self._detail_payload(manager, store, result.item))

    @PageApiUtils.guarded
    async def archive_object(self):
        manager, store = self._components()
        actor = self.utils.require_actor_username()
        payload = await self.utils.request_payload()
        owner_user_id = self.utils.required_text(payload, "owner_user_id")
        memory_item_id = self.utils.required_text(payload, "memory_item_id")
        expected_version = self.utils.required_int(payload, "expected_version", minimum=1)
        _item, context = await self._item(manager, store, owner_user_id, memory_item_id)
        result = await manager.archive(
            context=context,
            memory_item_id=memory_item_id,
            expected_version=expected_version,
            operation_key=self.utils.operation_key("object-archive", actor),
            actor_type=MemoryActorType.ADMIN,
            actor_id=actor,
            reason=self.utils.optional_text(payload.get("reason")),
        )
        return self.utils.ok(await self._detail_payload(manager, store, result.item))

    @PageApiUtils.guarded
    async def batch_objects(self):
        manager, store = self._components()
        actor = self.utils.require_actor_username()
        payload = await self.utils.request_payload()
        owner_user_id = self.utils.required_text(payload, "owner_user_id")
        action = self.utils.required_text(payload, "action")
        raw_items = payload.get("items")
        if not isinstance(raw_items, list) or not raw_items:
            raise PageApiProblem(
                "items 必须是非空数组",
                status=400,
                code="MEMORY_INVALID_REQUEST",
            )
        selected: list[tuple[MemoryItem, Any, int]] = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                raise PageApiProblem(
                    "items 元素必须是对象",
                    status=400,
                    code="MEMORY_INVALID_REQUEST",
                )
            item_id = self.utils.required_text(raw, "memory_item_id")
            expected_version = self.utils.required_int(raw, "expected_version", minimum=1)
            item, context = await self._item(manager, store, owner_user_id, item_id)
            if item.version != expected_version:
                raise EvolvingMemoryVersionConflictError(item_id, expected_version, item.version)
            selected.append((item, context, expected_version))
        if action not in {"archive", "index_retry"}:
            raise PageApiProblem(
                "action 必须是 archive 或 index_retry",
                status=422,
                code="MEMORY_VALIDATION_FAILED",
            )
        updated_count = 0
        for item, context, expected_version in selected:
            if action == "archive":
                await manager.archive(
                    context=context,
                    memory_item_id=item.memory_item_id,
                    expected_version=expected_version,
                    operation_key=self.utils.operation_key("object-batch-archive", actor),
                    actor_type=MemoryActorType.ADMIN,
                    actor_id=actor,
                )
                updated_count += 1
            else:
                updated_count += await store.retry_index_items(
                    context=context,
                    expected_versions={item.memory_item_id: expected_version},
                )
        return self.utils.ok({"updated_count": updated_count, "action": action})

    @PageApiUtils.guarded
    async def list_conflicts(self):
        self.utils.require_actor_username()
        manager, store = self._components()
        owner_user_id = self.utils.required_text(
            {"owner_user_id": self.utils.query_value("owner_user_id")},
            "owner_user_id",
        )
        await manager.build_admin_access_context(owner_user_id=owner_user_id)
        raw_status = self.utils.optional_text(self.utils.query_value("status"))
        status = None
        if raw_status:
            try:
                status = ConflictStatus(raw_status)
            except ValueError as exc:
                raise PageApiProblem(
                    "conflict status 无效",
                    status=422,
                    code="MEMORY_VALIDATION_FAILED",
                ) from exc
        conflicts = await store.list_conflicts(
            owner_user_id=owner_user_id,
            status=status,
            limit=500,
        )
        return self.utils.ok(
            {
                "conflicts": [
                    await self._conflict_payload(manager, store, conflict)
                    for conflict in conflicts
                ]
            }
        )

    @PageApiUtils.guarded
    async def resolve_conflict(self):
        manager, store = self._components()
        actor = self.utils.require_actor_username()
        payload = await self.utils.request_payload()
        owner_user_id = self.utils.required_text(payload, "owner_user_id")
        conflict_id = self.utils.required_text(payload, "conflict_id")
        action = self.utils.required_text(payload, "action")
        expected_versions = self.utils.expected_versions(payload.get("expected_versions"))
        conflict = await store.get_conflict(
            owner_user_id=owner_user_id,
            conflict_id=conflict_id,
        )
        if conflict is None:
            raise EvolvingMemoryNotFoundError("冲突记录不存在")
        _left, context = await self._item(manager, store, owner_user_id, conflict.left_item_id)
        if action not in {"merge", "supersede_left", "supersede_right", "dismiss"}:
            raise PageApiProblem(
                "action 必须是 merge、supersede_left、supersede_right 或 dismiss",
                status=422,
                code="MEMORY_VALIDATION_FAILED",
            )
        survivor_id = self.utils.optional_text(payload.get("survivor_memory_item_id"))
        if action == "merge" and survivor_id is not None and survivor_id not in {
            conflict.left_item_id,
            conflict.right_item_id,
        }:
            raise PageApiProblem(
                "survivor_memory_item_id 必须属于冲突两端",
                status=422,
                code="MEMORY_VALIDATION_FAILED",
            )
        resolved, result_item = await store.resolve_conflict(
            context=context,
            conflict_id=conflict_id,
            action=action,
            expected_versions=expected_versions,
            operation_key=self.utils.operation_key(f"conflict-{action}", actor),
            resolved_by=actor,
            resolution_note=self.utils.optional_text(payload.get("reason")),
            survivor_item_id=survivor_id,
            content=self.utils.optional_text(payload.get("content")),
        )
        data: dict[str, Any] = {
            "conflict": await self._conflict_payload(manager, store, resolved),
        }
        if result_item is not None:
            data["detail"] = await self._detail_payload(manager, store, result_item)
        return self.utils.ok(data)


__all__ = ["MemoryObjectHandler"]
