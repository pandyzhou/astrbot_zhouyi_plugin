from __future__ import annotations

import json
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

import aiosqlite
from quart import Quart
from starlette.requests import Request

ASTRBOT_ROOT = Path(__file__).resolve().parents[4]
if str(ASTRBOT_ROOT) not in sys.path:
    sys.path.insert(0, str(ASTRBOT_ROOT))

from astrbot.api.web import PluginRequest, bind_request_context
from data.plugins.astrbot_zhouyi_plugin.memory.core.managers.evolving_memory_manager import (
    EvolvingMemoryManager,
)
from data.plugins.astrbot_zhouyi_plugin.memory.core.models.evolving_memory import IndexStatus
from data.plugins.astrbot_zhouyi_plugin.memory.core.page_api_modules import (
    MemoryHandler,
    MemoryObjectHandler,
    PageApiUtils,
)
from data.plugins.astrbot_zhouyi_plugin.memory.storage.evolving_memory_store import (
    EvolvingMemoryStore,
)


@contextmanager
def plugin_request(
    *,
    method: str,
    path: str,
    query: dict[str, object] | None = None,
    payload: dict[str, object] | None = None,
    username: str | None = "operator",
):
    body = json.dumps(payload or {}, ensure_ascii=False).encode()
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    headers = [(b"content-type", b"application/json")]
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": urlencode(query or {}).encode(),
        "headers": headers,
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }
    request = Request(scope, receive)
    with bind_request_context(PluginRequest(request, username=username)):
        yield


def response_payload(response):
    if isinstance(response, dict):
        return 200, response
    return response.status_code, json.loads(response.body.decode("utf-8"))


class MemoryObjectApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        temp_root = Path(__file__).resolve().parents[1] / "temp"
        temp_root.mkdir(parents=True, exist_ok=True)
        self.temp_dir = tempfile.TemporaryDirectory(dir=temp_root)
        self.store = EvolvingMemoryStore(str(Path(self.temp_dir.name) / "objects.db"))
        self.manager = EvolvingMemoryManager(self.store)
        await self.manager.initialize()
        self.owner_context = await self.manager.build_access_context(
            platform_id="qq",
            bot_id="main",
            external_user_id="10001",
            session_id="qq:FriendMessage:10001",
            persona_id="default",
            is_group=False,
        )
        self.other_context = await self.manager.build_access_context(
            platform_id="qq",
            bot_id="main",
            external_user_id="10002",
            session_id="qq:FriendMessage:10002",
            persona_id="default",
            is_group=False,
        )
        service = SimpleNamespace(
            bootstrap=SimpleNamespace(
                evolving_memory_manager=self.manager,
                evolving_memory_store=self.store,
            )
        )
        self.handler = MemoryObjectHandler(PageApiUtils(), service)

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def call(self, method_name: str, *, method="GET", query=None, payload=None, username="operator"):
        with plugin_request(
            method=method,
            path=f"/page/v1/memory/{method_name}",
            query=query,
            payload=payload,
            username=username,
        ):
            return response_payload(await getattr(self.handler, method_name.replace("/", "_"), None)())

    async def create(self, content: str, *, source=None):
        payload = {
            "owner_user_id": self.owner_context.owner_user_id,
            "expected_version": 0,
            "scope": "user",
            "content": content,
            "memory_type": "PREFERENCE",
            "canonical_key": content,
            "importance": 0.8,
            "confidence": 0.9,
        }
        if source is not None:
            payload["source"] = source
        with plugin_request(method="POST", path="/objects/create", payload=payload):
            status, body = response_payload(await self.handler.create_object())
        self.assertEqual(status, 200, body)
        return body["data"]["item"]

    async def test_owner_scoped_crud_revision_source_and_actor(self):
        item = await self.create(
            "喜欢南瓜汤",
            source={
                "source_key": "message:1-2",
                "source_type": "message_range",
                "session_id": self.owner_context.session_id,
                "message_start_id": 1,
                "message_end_id": 2,
                "content_snapshot": "用户说喜欢南瓜汤",
                "metadata": {"platform_id": "qq"},
            },
        )
        self.assertEqual(item["item_type"], "PREFERENCE")
        self.assertEqual(item["memory_type"], "PREFERENCE")
        self.assertEqual(item["owner_user_id"], self.owner_context.owner_user_id)
        self.assertTrue(
            {
                "memory_item_id",
                "owner_user_id",
                "owner_display_name",
                "scope",
                "session_id",
                "persona_id",
                "item_type",
                "memory_type",
                "canonical_key",
                "status",
                "content",
                "structured_payload",
                "current_revision_no",
                "version",
                "importance",
                "confidence",
                "useful_score",
                "group_safe",
                "current_document_id",
                "index_status",
                "conflict_count",
                "source_count",
                "relation_count",
                "created_at",
                "updated_at",
            }.issubset(item)
        )

        with plugin_request(
            method="GET",
            path="/objects",
            query={"owner_user_id": self.owner_context.owner_user_id, "page": 1, "page_size": 20},
        ):
            status, listed = response_payload(await self.handler.list_objects())
        self.assertEqual(status, 200)
        self.assertEqual([entry["memory_item_id"] for entry in listed["data"]["items"]], [item["memory_item_id"]])

        with plugin_request(
            method="GET",
            path="/objects",
            query={"owner_user_id": self.other_context.owner_user_id},
        ):
            _, other = response_payload(await self.handler.list_objects())
        self.assertEqual(other["data"]["items"], [])

        with plugin_request(
            method="POST",
            path="/objects/update",
            payload={
                "owner_user_id": self.owner_context.owner_user_id,
                "memory_item_id": item["memory_item_id"],
                "expected_version": item["version"],
                "scope": "persona",
                "persona_id": "default",
                "content": "最喜欢香浓南瓜汤",
                "memory_type": "PREFERENCE",
                "reason": "operator edit",
            },
        ):
            status, updated = response_payload(await self.handler.update_object())
        self.assertEqual(status, 200, updated)
        next_item = updated["data"]["item"]
        self.assertEqual(next_item["memory_item_id"], item["memory_item_id"])
        self.assertEqual(next_item["version"], 2)
        self.assertEqual(next_item["scope"], "persona")

        common_query = {
            "owner_user_id": self.owner_context.owner_user_id,
            "memory_item_id": item["memory_item_id"],
        }
        with plugin_request(method="GET", path="/objects/revisions", query=common_query):
            _, revisions = response_payload(await self.handler.list_revisions())
        self.assertEqual([entry["revision_no"] for entry in revisions["data"]["revisions"]], [2, 1])
        self.assertEqual(revisions["data"]["revisions"][0]["actor"], "operator")
        self.assertTrue(
            {
                "memory_item_id",
                "revision_no",
                "operation",
                "content",
                "structured_payload",
                "base_version",
                "actor",
                "reason",
                "created_at",
            }.issubset(revisions["data"]["revisions"][0])
        )

        with plugin_request(method="GET", path="/objects/sources", query=common_query):
            _, sources = response_payload(await self.handler.list_sources())
        self.assertEqual(len(sources["data"]["sources"]), 1)
        self.assertEqual(sources["data"]["sources"][0]["platform_id"], "qq")
        self.assertTrue(
            {
                "source_id",
                "revision_no",
                "source_type",
                "document_id",
                "message_id_start",
                "message_id_end",
                "session_id",
                "platform_id",
                "content_snapshot",
                "availability",
                "created_at",
            }.issubset(sources["data"]["sources"][0])
        )

        with plugin_request(method="GET", path="/objects/detail", query=common_query):
            _, detail = response_payload(await self.handler.get_object_detail())
        self.assertEqual(detail["data"]["item"]["content"], "最喜欢香浓南瓜汤")
        self.assertEqual(set(detail["data"]), {"item", "relations", "conflicts"})

    async def test_merge_supersede_archive_and_batch_index_retry(self):
        first = await self.create("喜欢草莓蛋糕")
        second = await self.create("也喜欢草莓奶油蛋糕")
        owner = self.owner_context.owner_user_id
        expected = {
            first["memory_item_id"]: first["version"],
            second["memory_item_id"]: second["version"],
        }
        merge_base = {
            "owner_user_id": owner,
            "survivor_memory_item_id": first["memory_item_id"],
            "source_memory_item_ids": [second["memory_item_id"]],
            "expected_versions": expected,
        }
        with plugin_request(method="POST", path="/objects/merge/preview", payload=merge_base):
            status, preview = response_payload(await self.handler.preview_merge())
        self.assertEqual(status, 200, preview)
        self.assertIn("草莓蛋糕", preview["data"]["merged_content"])

        with plugin_request(
            method="POST",
            path="/objects/merge",
            payload={**merge_base, "content": "喜欢草莓奶油蛋糕"},
        ):
            status, merged = response_payload(await self.handler.merge_objects())
        self.assertEqual(status, 200, merged)
        merged_item = merged["data"]["item"]
        source_after = await self.store.get_item(
            owner_user_id=owner,
            memory_item_id=second["memory_item_id"],
        )
        self.assertEqual(source_after.status.value, "superseded")

        replacement = await self.create("现在更喜欢蓝莓蛋糕")
        supersede_expected = {
            merged_item["memory_item_id"]: merged_item["version"],
            replacement["memory_item_id"]: replacement["version"],
        }
        with plugin_request(
            method="POST",
            path="/objects/supersede",
            payload={
                "owner_user_id": owner,
                "old_memory_item_id": merged_item["memory_item_id"],
                "new_memory_item_id": replacement["memory_item_id"],
                "expected_versions": supersede_expected,
            },
        ):
            status, superseded = response_payload(await self.handler.supersede_object())
        self.assertEqual(status, 200, superseded)
        replacement_item = superseded["data"]["item"]

        with plugin_request(
            method="POST",
            path="/objects/batch",
            payload={
                "owner_user_id": owner,
                "action": "index_retry",
                "items": [{"memory_item_id": replacement_item["memory_item_id"], "expected_version": replacement_item["version"]}],
            },
        ):
            status, retried = response_payload(await self.handler.batch_objects())
        self.assertEqual(status, 200, retried)
        self.assertEqual(retried["data"]["updated_count"], 1)

        with plugin_request(
            method="POST",
            path="/objects/archive",
            payload={
                "owner_user_id": owner,
                "memory_item_id": replacement_item["memory_item_id"],
                "expected_version": replacement_item["version"],
            },
        ):
            status, archived = response_payload(await self.handler.archive_object())
        self.assertEqual(status, 200, archived)
        self.assertEqual(archived["data"]["item"]["status"], "archived")

    async def test_legacy_document_detail_and_content_update_use_stable_object_revision(self):
        item_payload = await self.create("旧 API 关联内容")
        item = await self.store.get_item(
            owner_user_id=self.owner_context.owner_user_id,
            memory_item_id=item_payload["memory_item_id"],
        )
        async with aiosqlite.connect(self.store.db_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS documents(
                    id INTEGER PRIMARY KEY,
                    doc_id TEXT,
                    text TEXT,
                    metadata TEXT,
                    created_at TEXT,
                    updated_at TEXT
                )
                """
            )
            await db.execute(
                "INSERT INTO documents(id, doc_id, text, metadata, created_at, updated_at) VALUES (1, 'doc-1', ?, '{}', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
                (item.content,),
            )
            await db.commit()
        await self.store.set_index_status(
            owner_user_id=item.owner_user_id,
            memory_item_id=item.memory_item_id,
            status=IndexStatus.CURRENT,
            current_document_id=1,
        )

        class Engine:
            db_path = self.store.db_path
            graph_store = None

            def __init__(self):
                self.deleted = []

            async def delete_memory(self, memory_id):
                self.deleted.append(memory_id)
                return True

        engine = Engine()
        service = SimpleNamespace(
            bootstrap=SimpleNamespace(
                evolving_memory_manager=self.manager,
                evolving_memory_store=self.store,
            )
        )
        legacy_handler = MemoryHandler(PageApiUtils(), service)
        app = Quart(__name__)
        async with app.test_request_context("/memories/detail?memory_id=1", method="GET"):
            detail = await legacy_handler.get_memory_detail(engine)
        self.assertEqual(detail["data"]["memory_item_id"], item.memory_item_id)
        self.assertEqual(detail["data"]["item_type"], item.item_type)

        update_payload = {
            "memory_id": 1,
            "owner_user_id": item.owner_user_id,
            "expected_version": item.version,
            "field": "content",
            "value": "旧 API 通过 revision 更新后的内容",
            "reason": "legacy adapter test",
        }
        async with app.test_request_context("/memories/update", method="POST", json=update_payload):
            with plugin_request(method="POST", path="/memories/update", payload=update_payload):
                status, updated = response_payload(await legacy_handler.update_memory(engine))
        self.assertEqual(status, 200, updated)
        self.assertEqual(updated["data"]["memory_item_id"], item.memory_item_id)
        self.assertEqual(engine.deleted, [])
        revisions = await self.store.list_revisions(
            owner_user_id=item.owner_user_id,
            memory_item_id=item.memory_item_id,
        )
        self.assertEqual(revisions[0].content, "旧 API 通过 revision 更新后的内容")
        self.assertEqual(revisions[0].actor_id, "operator")

    async def test_object_update_cannot_move_across_owner_boundary(self):
        item = await self.create("禁止跨 owner 移动")
        with plugin_request(
            method="POST",
            path="/objects/update",
            payload={
                "owner_user_id": self.owner_context.owner_user_id,
                "target_owner_user_id": self.other_context.owner_user_id,
                "memory_item_id": item["memory_item_id"],
                "expected_version": item["version"],
                "content": "尝试跨 owner",
            },
        ):
            status, denied = response_payload(await self.handler.update_object())
        self.assertEqual(status, 422)
        self.assertEqual(denied["data"]["code"], "MEMORY_ACCESS_CONTEXT_INVALID")
        current = await self.store.get_item(
            owner_user_id=self.owner_context.owner_user_id,
            memory_item_id=item["memory_item_id"],
        )
        moved = await self.store.get_item(
            owner_user_id=self.other_context.owner_user_id,
            memory_item_id=item["memory_item_id"],
        )
        self.assertEqual(current.version, item["version"])
        self.assertIsNone(moved)

    async def test_create_requires_expected_version_and_admin_reads_require_actor(self):
        with plugin_request(
            method="POST",
            path="/objects/create",
            payload={
                "owner_user_id": self.owner_context.owner_user_id,
                "scope": "user",
                "content": "缺少并发基线",
                "memory_type": "FACT",
            },
        ):
            status, missing_version = response_payload(await self.handler.create_object())
        self.assertEqual(status, 400)
        self.assertEqual(missing_version["data"]["code"], "MEMORY_INVALID_REQUEST")

        with plugin_request(
            method="GET",
            path="/objects",
            query={"owner_user_id": self.owner_context.owner_user_id},
            username=None,
        ):
            status, unauthenticated = response_payload(await self.handler.list_objects())
        self.assertEqual(status, 401)
        self.assertEqual(unauthenticated["data"]["code"], "AUTH_REQUIRED")

    async def test_owner_is_required_and_cross_owner_detail_is_not_found(self):
        item = await self.create("owner isolated")
        with plugin_request(method="GET", path="/objects/detail", query={"memory_item_id": item["memory_item_id"]}):
            status, missing_owner = response_payload(await self.handler.get_object_detail())
        self.assertEqual(status, 400)
        self.assertEqual(missing_owner["data"]["code"], "MEMORY_INVALID_REQUEST")

        with plugin_request(
            method="GET",
            path="/objects/detail",
            query={
                "owner_user_id": self.other_context.owner_user_id,
                "memory_item_id": item["memory_item_id"],
            },
        ):
            status, denied = response_payload(await self.handler.get_object_detail())
        self.assertEqual(status, 404)
        self.assertEqual(denied["data"]["code"], "MEMORY_OBJECT_NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
