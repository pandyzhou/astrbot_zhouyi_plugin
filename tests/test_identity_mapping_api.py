from __future__ import annotations

import json
import sys
import tempfile
import unittest
from contextlib import contextmanager

import aiosqlite
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

from starlette.requests import Request

ASTRBOT_ROOT = Path(__file__).resolve().parents[4]
if str(ASTRBOT_ROOT) not in sys.path:
    sys.path.insert(0, str(ASTRBOT_ROOT))

from astrbot.api.web import PluginRequest, bind_request_context
from data.plugins.astrbot_zhouyi_plugin.memory.core.managers.evolving_memory_manager import EvolvingMemoryManager
from data.plugins.astrbot_zhouyi_plugin.memory.core.models.evolving_memory import MemoryActorType, MemoryScope
from data.plugins.astrbot_zhouyi_plugin.memory.core.page_api_modules import IdentityHandler, PageApiUtils
from data.plugins.astrbot_zhouyi_plugin.memory.storage.evolving_memory_store import EvolvingMemoryStore


@contextmanager
def request_context(
    method: str,
    path: str,
    *,
    payload=None,
    query=None,
    username="operator",
    headers=None,
):
    body = json.dumps(payload or {}, ensure_ascii=False).encode()
    consumed = False

    async def receive():
        nonlocal consumed
        if consumed:
            return {"type": "http.request", "body": b"", "more_body": False}
        consumed = True
        return {"type": "http.request", "body": body, "more_body": False}

    request_headers = [(b"content-type", b"application/json")]
    request_headers.extend(
        (str(name).lower().encode(), str(value).encode())
        for name, value in (headers or {}).items()
    )
    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": urlencode(query or {}).encode(),
            "headers": request_headers,
            "client": ("127.0.0.1", 1),
            "server": ("test", 80),
        },
        receive,
    )
    with bind_request_context(PluginRequest(request, username=username)):
        yield


def unpack(response):
    if isinstance(response, dict):
        return 200, response
    return response.status_code, json.loads(response.body.decode())


class IdentityMappingApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp_root = Path(__file__).resolve().parents[1] / "temp"
        temp_root.mkdir(parents=True, exist_ok=True)
        self.temp_dir = tempfile.TemporaryDirectory(dir=temp_root)
        self.store = EvolvingMemoryStore(str(Path(self.temp_dir.name) / "identity.db"))
        self.manager = EvolvingMemoryManager(self.store)
        await self.manager.initialize()
        self.first = await self.manager.build_access_context(
            platform_id="qq",
            bot_id="main",
            external_user_id="30001",
            session_id="qq:FriendMessage:30001",
            persona_id="default",
            is_group=False,
        )
        self.second = await self.manager.build_access_context(
            platform_id="telegram",
            bot_id="main",
            external_user_id="xingyao",
            session_id="telegram:FriendMessage:xingyao",
            persona_id="default",
            is_group=False,
        )
        service = SimpleNamespace(
            bootstrap=SimpleNamespace(
                evolving_memory_manager=self.manager,
                evolving_memory_store=self.store,
            )
        )
        self.handler = IdentityHandler(PageApiUtils(), service)

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    async def test_list_link_conflict_and_explicit_alias_move(self):
        with request_context("GET", "/identities"):
            status, identities = unpack(await self.handler.list_identities())
        self.assertEqual(status, 200)
        self.assertEqual(identities["data"]["total"], 2)
        owner = next(item for item in identities["data"]["owners"] if item["owner_user_id"] == self.first.owner_user_id)
        self.assertEqual(owner["aliases"][0]["external_user_id"], "30001")
        self.assertTrue(
            {"owner_user_id", "display_name", "status", "aliases", "created_at", "updated_at"}.issubset(owner)
        )
        self.assertTrue(
            {
                "identity_link_id",
                "owner_user_id",
                "platform_id",
                "bot_id",
                "external_user_id",
                "verified",
                "source",
                "status",
                "created_at",
            }.issubset(owner["aliases"][0])
        )

        with request_context(
            "POST",
            "/identities/aliases/link",
            payload={
                "owner_user_id": self.second.owner_user_id,
                "platform_id": "discord",
                "bot_id": "main",
                "external_user_id": "alias-user",
                "verified": True,
            },
        ):
            status, linked = unpack(await self.handler.link_alias())
        self.assertEqual(status, 200, linked)
        link_id = linked["data"]["alias"]["identity_link_id"]

        with request_context(
            "POST",
            "/identities/aliases/link",
            payload={
                "owner_user_id": self.first.owner_user_id,
                "platform_id": "discord",
                "bot_id": "main",
                "external_user_id": "alias-user",
            },
        ):
            status, conflict = unpack(await self.handler.link_alias())
        self.assertEqual(status, 409)
        self.assertEqual(conflict["data"]["code"], "MEMORY_ALIAS_CONFLICT")

        with request_context(
            "POST",
            "/identities/aliases/move",
            payload={
                "identity_link_id": link_id,
                "owner_user_id": self.first.owner_user_id,
                "expected_owner_user_id": self.second.owner_user_id,
            },
        ):
            status, moved = unpack(await self.handler.move_alias())
        self.assertEqual(status, 200, moved)
        self.assertEqual(moved["data"]["alias"]["owner_user_id"], self.first.owner_user_id)

        with request_context(
            "POST",
            "/identities/aliases/move",
            payload={
                "identity_link_id": link_id,
                "owner_user_id": self.second.owner_user_id,
            },
        ):
            status, missing_expected_owner = unpack(await self.handler.move_alias())
        self.assertEqual(status, 400)
        self.assertEqual(missing_expected_owner["data"]["code"], "MEMORY_INVALID_REQUEST")

        with request_context(
            "POST",
            "/identities/aliases/move",
            payload={
                "identity_link_id": link_id,
                "owner_user_id": self.second.owner_user_id,
                "expected_owner_user_id": self.second.owner_user_id,
            },
        ):
            status, stale_owner = unpack(await self.handler.move_alias())
        self.assertEqual(status, 409)
        self.assertEqual(stale_owner["data"]["code"], "MEMORY_STATE_CONFLICT")

    async def test_page_idempotency_header_is_stable_and_batch_safe(self):
        headers = {"Idempotency-Key": "dashboard-retry-key"}
        with request_context("POST", "/test", headers=headers):
            first_keys = [
                PageApiUtils.operation_key("batch-archive", "operator")
                for _ in range(2)
            ]
        with request_context("POST", "/test", headers=headers):
            replay_keys = [
                PageApiUtils.operation_key("batch-archive", "operator")
                for _ in range(2)
            ]
        self.assertEqual(first_keys, replay_keys)
        self.assertNotEqual(first_keys[0], first_keys[1])

        with request_context("POST", "/test"):
            fallback_keys = [
                PageApiUtils.operation_key("object-create", "operator")
                for _ in range(2)
            ]
        self.assertNotEqual(fallback_keys[0], fallback_keys[1])

    async def test_admin_context_never_reuses_external_identity(self):
        context = await self.manager.build_admin_access_context(
            owner_user_id=self.first.owner_user_id,
            session_id=self.first.session_id,
            persona_id=self.first.persona_id,
        )
        self.assertEqual(context.platform_id, "internal-admin")
        self.assertEqual(context.bot_id, "dashboard")
        self.assertEqual(context.external_user_id, self.first.owner_user_id)

    async def test_owner_merge_requires_preview_and_expected_owner_states(self):
        source_owner = await self.store.create_owner(display_name="Legacy owner")
        source_context = await self.manager.build_admin_access_context(
            owner_user_id=source_owner.owner_user_id,
        )
        created = await self.manager.create(
            context=source_context,
            content="待迁移 owner 记忆",
            operation_key="identity-test:create",
            expected_version=0,
            scope=MemoryScope.USER,
            actor_type=MemoryActorType.ADMIN,
            actor_id="operator",
        )
        updated = await self.manager.update(
            context=source_context,
            memory_item_id=created.item.memory_item_id,
            expected_version=created.item.version,
            operation_key="identity-test:update",
            actor_type=MemoryActorType.ADMIN,
            actor_id="operator",
            content="待迁移 owner 记忆（已更新）",
        )
        preview_request = {
            "survivor_owner_user_id": self.first.owner_user_id,
            "source_owner_user_ids": [source_owner.owner_user_id],
        }
        with request_context("POST", "/identities/owners/merge/preview", payload=preview_request):
            status, preview = unpack(await self.handler.preview_owner_merge())
        self.assertEqual(status, 200, preview)
        self.assertEqual(preview["data"]["memory_item_count"], 1)
        self.assertTrue(preview["data"]["preview_id"].startswith("owner_merge_"))

        with request_context("POST", "/identities/owners/merge", payload=preview_request):
            status, missing_preview = unpack(await self.handler.merge_owners())
        self.assertEqual(status, 400)
        self.assertEqual(missing_preview["data"]["code"], "MEMORY_INVALID_REQUEST")

        merge_payload = {
            **preview_request,
            "preview_id": preview["data"]["preview_id"],
            "expected_owner_states": preview["data"]["expected_owner_states"],
        }
        idempotency_headers = {"Idempotency-Key": "owner-merge-stable-key"}
        with request_context(
            "POST",
            "/identities/owners/merge",
            payload=merge_payload,
            headers=idempotency_headers,
        ):
            status, merged = unpack(await self.handler.merge_owners())
        self.assertEqual(status, 200, merged)

        with request_context(
            "POST",
            "/identities/owners/merge",
            payload=merge_payload,
            headers=idempotency_headers,
        ):
            replay_status, replayed = unpack(await self.handler.merge_owners())
        self.assertEqual(replay_status, 200, replayed)
        self.assertTrue(replayed["data"]["merged"])

        with request_context(
            "POST",
            "/identities/owners/merge",
            payload={**merge_payload, "preview_id": f'{merge_payload["preview_id"]}-mismatch'},
            headers=idempotency_headers,
        ):
            mismatch_status, mismatch = unpack(await self.handler.merge_owners())
        self.assertEqual(mismatch_status, 409, mismatch)
        self.assertEqual(mismatch["data"]["code"], "MEMORY_STATE_CONFLICT")

        moved_item = await self.store.get_item(
            owner_user_id=self.first.owner_user_id,
            memory_item_id=created.item.memory_item_id,
        )
        self.assertIsNotNone(moved_item)
        self.assertEqual(moved_item.version, updated.item.version)
        revisions = await self.store.list_revisions(
            owner_user_id=self.first.owner_user_id,
            memory_item_id=created.item.memory_item_id,
        )
        self.assertEqual([revision.revision_no for revision in revisions], [2, 1])
        self.assertEqual(
            {revision.owner_user_id for revision in revisions},
            {self.first.owner_user_id},
        )
        self.assertEqual(revisions[0].content, "待迁移 owner 记忆（已更新）")
        self.assertEqual(revisions[1].content, "待迁移 owner 记忆")
        async with aiosqlite.connect(self.store.db_path) as db:
            with self.assertRaises(aiosqlite.IntegrityError):
                await db.execute(
                    "UPDATE memory_item_revisions SET owner_user_id = ? WHERE memory_item_id = ?",
                    (source_owner.owner_user_id, created.item.memory_item_id),
                )
        source_after = await self.store.get_owner(source_owner.owner_user_id)
        self.assertEqual(source_after.status.value, "merged")

    async def test_owner_merge_preview_is_invalidated_by_alias_move(self):
        source_owner = await self.store.create_owner(display_name="Alias source")
        link = await self.manager.link_identity(
            owner_user_id=source_owner.owner_user_id,
            platform_id="discord",
            bot_id="main",
            external_user_id="merge-race",
            verified=True,
            actor_source="admin",
        )
        request = {
            "survivor_owner_user_id": self.first.owner_user_id,
            "source_owner_user_ids": [source_owner.owner_user_id],
        }
        with request_context("POST", "/identities/owners/merge/preview", payload=request):
            status, preview = unpack(await self.handler.preview_owner_merge())
        self.assertEqual(status, 200, preview)
        self.assertIn("alias_count", preview["data"]["expected_owner_states"][source_owner.owner_user_id])

        await self.store.move_identity_link(
            identity_link_id=link.identity_link_id,
            owner_user_id=self.first.owner_user_id,
            expected_owner_user_id=source_owner.owner_user_id,
        )
        with request_context(
            "POST",
            "/identities/owners/merge",
            payload={
                **request,
                "preview_id": preview["data"]["preview_id"],
                "expected_owner_states": preview["data"]["expected_owner_states"],
            },
        ):
            status, stale = unpack(await self.handler.merge_owners())
        self.assertEqual(status, 409)
        self.assertEqual(stale["data"]["code"], "MEMORY_STATE_CONFLICT")
        source_after = await self.store.get_owner(source_owner.owner_user_id)
        self.assertEqual(source_after.status.value, "active")

    async def test_owner_update_supports_expected_state(self):
        owner = await self.store.get_owner(self.first.owner_user_id)
        with request_context(
            "POST",
            "/identities/owners/update",
            payload={
                "owner_user_id": owner.owner_user_id,
                "display_name": "周易",
                "status": "active",
                "expected_updated_at": owner.updated_at,
            },
        ):
            status, updated = unpack(await self.handler.update_owner())
        self.assertEqual(status, 200, updated)
        self.assertEqual(updated["data"]["owner"]["display_name"], "周易")


if __name__ == "__main__":
    unittest.main()
