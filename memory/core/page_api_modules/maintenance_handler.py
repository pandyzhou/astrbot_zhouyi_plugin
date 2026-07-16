"""Maintenance status and owner-scoped index retry Page API."""

from __future__ import annotations

from typing import Any

from ..base.exceptions import EvolvingMemoryNotFoundError, EvolvingMemoryVersionConflictError
from .utils import PageApiProblem, PageApiUtils


class MaintenanceHandler:
    def __init__(self, utils: PageApiUtils, memory_service: Any) -> None:
        self.utils = utils
        self.memory_service = memory_service

    def _components(self):
        return self.utils.resolve_evolving_components(self.memory_service)

    @PageApiUtils.guarded
    async def get_maintenance_status(self):
        self.utils.require_actor_username()
        _manager, store = self._components()
        return self.utils.ok(await store.maintenance_status())

    @PageApiUtils.guarded
    async def retry_index(self):
        manager, store = self._components()
        self.utils.require_actor_username()
        payload = await self.utils.request_payload()
        owner_user_id = self.utils.required_text(payload, "owner_user_id")
        expected_versions: dict[str, int]
        raw_items = payload.get("items")
        if isinstance(raw_items, list):
            expected_versions = {}
            for raw in raw_items:
                if not isinstance(raw, dict):
                    raise PageApiProblem(
                        "items 元素必须是对象",
                        status=400,
                        code="MEMORY_INVALID_REQUEST",
                    )
                item_id = self.utils.required_text(raw, "memory_item_id")
                expected_versions[item_id] = self.utils.required_int(
                    raw,
                    "expected_version",
                    minimum=1,
                )
        else:
            expected_versions = self.utils.expected_versions(payload.get("expected_versions"))
            raw_ids = payload.get("memory_item_ids")
            if raw_ids is not None:
                if not isinstance(raw_ids, list):
                    raise PageApiProblem(
                        "memory_item_ids 必须是数组",
                        status=400,
                        code="MEMORY_INVALID_REQUEST",
                    )
                requested_ids = {
                    value
                    for value in (self.utils.optional_text(item) for item in raw_ids)
                    if value is not None
                }
                if requested_ids != set(expected_versions):
                    raise PageApiProblem(
                        "memory_item_ids 必须与 expected_versions 完全一致",
                        status=422,
                        code="MEMORY_VALIDATION_FAILED",
                    )
        if not expected_versions:
            raise PageApiProblem(
                "至少提供一个待重试对象",
                status=400,
                code="MEMORY_INVALID_REQUEST",
            )
        selected = []
        for item_id, expected_version in expected_versions.items():
            item = await store.get_item(
                owner_user_id=owner_user_id,
                memory_item_id=item_id,
            )
            if item is None:
                raise EvolvingMemoryNotFoundError()
            if item.version != expected_version:
                raise EvolvingMemoryVersionConflictError(
                    item.memory_item_id,
                    expected_version,
                    item.version,
                )
            context = await manager.build_admin_access_context(
                owner_user_id=owner_user_id,
                session_id=item.session_id,
                persona_id=item.persona_id,
            )
            selected.append((item, context))
        queued_count = 0
        for item, context in selected:
            queued_count += await store.retry_index_items(
                context=context,
                expected_versions={item.memory_item_id: expected_versions[item.memory_item_id]},
            )
        return self.utils.ok(
            {
                "owner_user_id": owner_user_id,
                "queued_count": queued_count,
                "state": "pending",
            }
        )


__all__ = ["MaintenanceHandler"]
