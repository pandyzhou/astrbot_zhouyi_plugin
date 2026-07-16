"""Dashboard administration API for memory owners and identity aliases."""

from __future__ import annotations

from typing import Any

from ..base.exceptions import EvolvingMemoryAccessError, EvolvingMemoryNotFoundError
from ..models.evolving_memory import OwnerStatus
from .schemas import identity_alias_payload, owner_payload
from .utils import PageApiProblem, PageApiUtils


class IdentityHandler:
    def __init__(self, utils: PageApiUtils, memory_service: Any) -> None:
        self.utils = utils
        self.memory_service = memory_service

    def _components(self):
        return self.utils.resolve_evolving_components(self.memory_service)

    async def _identities_payload(self, store: Any) -> dict[str, Any]:
        owners = await store.list_owners(limit=1000)
        serialized = []
        for owner in owners:
            aliases = await store.list_identity_links(owner.owner_user_id)
            serialized.append(owner_payload(owner, aliases))
        return {
            "owners": serialized,
            "unmapped_aliases": [],
            "total": len(serialized),
        }

    @PageApiUtils.guarded
    async def list_identities(self):
        self.utils.require_actor_username()
        _manager, store = self._components()
        return self.utils.ok(await self._identities_payload(store))

    @PageApiUtils.guarded
    async def create_owner(self):
        _manager, store = self._components()
        self.utils.require_actor_username()
        payload = await self.utils.request_payload()
        display_name = self.utils.required_text(payload, "display_name")
        owner_user_id = self.utils.optional_text(payload.get("owner_user_id"))
        owner = await store.create_owner(
            owner_user_id=owner_user_id,
            display_name=display_name,
            metadata={"created_by": "dashboard"},
        )
        return self.utils.ok(
            {
                "owner": owner_payload(owner, []),
                "identities": await self._identities_payload(store),
            }
        )

    @PageApiUtils.guarded
    async def update_owner(self):
        manager, store = self._components()
        self.utils.require_actor_username()
        payload = await self.utils.request_payload()
        owner_user_id = self.utils.required_text(payload, "owner_user_id")
        await manager.build_admin_access_context(owner_user_id=owner_user_id)
        display_name = self.utils.optional_text(payload.get("display_name"))
        if display_name is None:
            display_name = owner_user_id
        try:
            status = OwnerStatus(self.utils.required_text(payload, "status"))
        except ValueError as exc:
            raise PageApiProblem(
                "owner status 无效",
                status=422,
                code="MEMORY_OWNER_STATUS_INVALID",
            ) from exc
        owner = await store.update_owner(
            owner_user_id=owner_user_id,
            display_name=display_name,
            status=status,
            expected_updated_at=self.utils.required_text(payload, "expected_updated_at"),
        )
        aliases = await store.list_identity_links(owner_user_id)
        return self.utils.ok({"owner": owner_payload(owner, aliases)})

    @PageApiUtils.guarded
    async def link_alias(self):
        manager, store = self._components()
        self.utils.require_actor_username()
        payload = await self.utils.request_payload()
        owner_user_id = self.utils.required_text(payload, "owner_user_id")
        await manager.build_admin_access_context(owner_user_id=owner_user_id)
        platform_id = self.utils.required_text(payload, "platform_id")
        bot_id = self.utils.required_text(payload, "bot_id")
        external_user_id = self.utils.required_text(payload, "external_user_id")
        existing = await store.resolve_identity(
            platform_id=platform_id,
            bot_id=bot_id,
            external_user_id=external_user_id,
            create_if_missing=False,
        )
        if existing is not None and existing.owner_user_id != owner_user_id:
            raise PageApiProblem(
                "identity alias 已绑定到其他 owner，必须使用显式移动操作",
                status=409,
                code="MEMORY_ALIAS_CONFLICT",
                details={"current_owner_user_id": existing.owner_user_id},
            )
        verified = payload.get("verified", True)
        if not isinstance(verified, bool):
            raise PageApiProblem(
                "verified 必须是布尔值",
                status=422,
                code="MEMORY_VALIDATION_FAILED",
            )
        try:
            link = await manager.link_identity(
                owner_user_id=owner_user_id,
                platform_id=platform_id,
                bot_id=bot_id,
                external_user_id=external_user_id,
                verified=verified,
                actor_source="admin",
            )
        except EvolvingMemoryAccessError as exc:
            raise PageApiProblem(
                str(exc),
                status=409,
                code="MEMORY_ALIAS_CONFLICT",
            ) from exc
        return self.utils.ok({"alias": identity_alias_payload(link)})

    @PageApiUtils.guarded
    async def move_alias(self):
        manager, store = self._components()
        self.utils.require_actor_username()
        payload = await self.utils.request_payload()
        owner_user_id = self.utils.required_text(payload, "owner_user_id")
        await manager.build_admin_access_context(owner_user_id=owner_user_id)
        identity_link_id = self.utils.required_int(payload, "identity_link_id", minimum=1)
        expected_owner_user_id = self.utils.required_text(payload, "expected_owner_user_id")
        link = await store.get_identity_link_by_id(identity_link_id)
        if link is None:
            raise EvolvingMemoryNotFoundError("identity link 不存在")
        await manager.build_admin_access_context(owner_user_id=expected_owner_user_id)
        moved = await store.move_identity_link(
            identity_link_id=identity_link_id,
            owner_user_id=owner_user_id,
            expected_owner_user_id=expected_owner_user_id,
        )
        return self.utils.ok(
            {
                "moved": True,
                "previous_owner_user_id": link.owner_user_id,
                "alias": identity_alias_payload(moved),
            }
        )

    @PageApiUtils.guarded
    async def preview_owner_merge(self):
        manager, store = self._components()
        self.utils.require_actor_username()
        payload = await self.utils.request_payload()
        survivor_owner_user_id = self.utils.required_text(payload, "survivor_owner_user_id")
        await manager.build_admin_access_context(owner_user_id=survivor_owner_user_id)
        raw_sources = payload.get("source_owner_user_ids")
        if not isinstance(raw_sources, list):
            raise PageApiProblem(
                "source_owner_user_ids 必须是数组",
                status=400,
                code="MEMORY_INVALID_REQUEST",
            )
        source_owner_user_ids = [
            value
            for value in (self.utils.optional_text(item) for item in raw_sources)
            if value is not None
        ]
        for owner_id in source_owner_user_ids:
            await manager.build_admin_access_context(owner_user_id=owner_id)
        return self.utils.ok(
            await store.preview_owner_merge(
                survivor_owner_user_id=survivor_owner_user_id,
                source_owner_user_ids=source_owner_user_ids,
            )
        )

    @PageApiUtils.guarded
    async def merge_owners(self):
        manager, store = self._components()
        actor = self.utils.require_actor_username()
        payload = await self.utils.request_payload()
        survivor_owner_user_id = self.utils.required_text(payload, "survivor_owner_user_id")
        await manager.build_admin_access_context(owner_user_id=survivor_owner_user_id)
        raw_sources = payload.get("source_owner_user_ids")
        if not isinstance(raw_sources, list):
            raise PageApiProblem(
                "source_owner_user_ids 必须是数组",
                status=400,
                code="MEMORY_INVALID_REQUEST",
            )
        source_owner_user_ids = [
            value
            for value in (self.utils.optional_text(item) for item in raw_sources)
            if value is not None
        ]
        for owner_id in source_owner_user_ids:
            await manager.build_admin_access_context(owner_user_id=owner_id)
        preview_id = self.utils.required_text(payload, "preview_id")
        expected_owner_states = payload.get("expected_owner_states")
        if not isinstance(expected_owner_states, dict):
            raise PageApiProblem(
                "expected_owner_states 必须来自 owner merge preview",
                status=400,
                code="MEMORY_INVALID_REQUEST",
            )
        normalized_states: dict[str, dict[str, str]] = {}
        for owner_id, raw_state in expected_owner_states.items():
            normalized_owner_id = self.utils.optional_text(owner_id)
            if normalized_owner_id is None or not isinstance(raw_state, dict):
                raise PageApiProblem(
                    "expected_owner_states 格式无效",
                    status=422,
                    code="MEMORY_VALIDATION_FAILED",
                )
            normalized_state = {
                str(key): str(value)
                for key, value in raw_state.items()
                if self.utils.optional_text(key) is not None
                and isinstance(value, (str, int))
                and not isinstance(value, bool)
            }
            self.utils.required_text(normalized_state, "status")
            self.utils.required_text(normalized_state, "updated_at")
            normalized_states[normalized_owner_id] = normalized_state
        result = await store.merge_owners(
            survivor_owner_user_id=survivor_owner_user_id,
            source_owner_user_ids=source_owner_user_ids,
            preview_id=preview_id,
            expected_owner_states=normalized_states,
            operation_key=self.utils.operation_key(
                "owner-merge",
                actor,
                payload.get("idempotency_key") or payload.get("operation_key"),
            ),
        )
        return self.utils.ok(
            {
                **result,
                "identities": await self._identities_payload(store),
            }
        )


__all__ = ["IdentityHandler"]
