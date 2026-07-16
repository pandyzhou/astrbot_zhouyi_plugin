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
from data.plugins.astrbot_zhouyi_plugin.memory.core.models.evolving_memory import IndexStatus, MemoryActorType, MemoryScope
from data.plugins.astrbot_zhouyi_plugin.memory.core.page_api_modules import MaintenanceHandler, MemoryObjectHandler, PageApiUtils
from data.plugins.astrbot_zhouyi_plugin.memory.storage.evolving_memory_store import EvolvingMemoryStore


@contextmanager
def request_context(method: str, path: str, *, payload=None, query=None, username="operator"):
    body = json.dumps(payload or {}, ensure_ascii=False).encode()
    consumed = False

    async def receive():
        nonlocal consumed
        if consumed:
            return {"type": "http.request", "body": b"", "more_body": False}
        consumed = True
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": urlencode(query or {}).encode(),
            "headers": [(b"content-type", b"application/json")],
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


class MemoryConflictsApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp_root = Path(__file__).resolve().parents[1] / "temp"
        temp_root.mkdir(parents=True, exist_ok=True)
        self.temp_dir = tempfile.TemporaryDirectory(dir=temp_root)
        self.store = EvolvingMemoryStore(str(Path(self.temp_dir.name) / "conflicts.db"))
        self.manager = EvolvingMemoryManager(self.store)
        await self.manager.initialize()
        self.context = await self.manager.build_access_context(
            platform_id="qq",
            bot_id="main",
            external_user_id="40001",
            session_id="qq:FriendMessage:40001",
            persona_id="default",
            is_group=False,
        )
        service = SimpleNamespace(
            bootstrap=SimpleNamespace(
                evolving_memory_manager=self.manager,
                evolving_memory_store=self.store,
            )
        )
        self.object_handler = MemoryObjectHandler(PageApiUtils(), service)
        self.maintenance_handler = MaintenanceHandler(PageApiUtils(), service)

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    async def create_pair(self, suffix: str):
        left = await self.manager.create(
            context=self.context,
            content=f"住在北京 {suffix}",
            operation_key=f"conflict:left:{suffix}",
            expected_version=0,
            scope=MemoryScope.USER,
            actor_type=MemoryActorType.USER,
            actor_id="tester",
        )
        right = await self.manager.create(
            context=self.context,
            content=f"住在上海 {suffix}",
            operation_key=f"conflict:right:{suffix}",
            expected_version=0,
            scope=MemoryScope.USER,
            actor_type=MemoryActorType.USER,
            actor_id="tester",
        )
        conflict = await self.store.create_conflict(
            context=self.context,
            left_item_id=left.item.memory_item_id,
            right_item_id=right.item.memory_item_id,
            expected_versions={
                left.item.memory_item_id: left.item.version,
                right.item.memory_item_id: right.item.version,
            },
            conflict_type="contradictory_location",
        )
        left_current = await self.store.get_item(owner_user_id=self.context.owner_user_id, memory_item_id=left.item.memory_item_id)
        right_current = await self.store.get_item(owner_user_id=self.context.owner_user_id, memory_item_id=right.item.memory_item_id)
        return conflict, left_current, right_current

    async def test_list_and_dismiss_conflict_then_lock_repeated_resolution(self):
        conflict, left, right = await self.create_pair("dismiss")
        owner = self.context.owner_user_id
        with request_context("GET", "/conflicts", query={"owner_user_id": owner, "status": "open"}):
            status, listed = unpack(await self.object_handler.list_conflicts())
        self.assertEqual(status, 200)
        listed_conflict = listed["data"]["conflicts"][0]
        self.assertEqual(listed_conflict["conflict_id"], conflict.conflict_id)
        self.assertEqual(listed_conflict["left_item"]["item_type"], "fact")
        self.assertEqual(listed_conflict["left_item"]["memory_type"], "fact")
        self.assertTrue(
            {
                "conflict_id",
                "conflict_type",
                "severity",
                "status",
                "left_item",
                "right_item",
                "resolution",
                "resolution_reason",
                "created_at",
                "resolved_at",
            }.issubset(listed_conflict)
        )

        resolution = {
            "owner_user_id": owner,
            "conflict_id": conflict.conflict_id,
            "action": "dismiss",
            "expected_versions": {
                left.memory_item_id: left.version,
                right.memory_item_id: right.version,
            },
            "reason": "人工确认暂不处理",
        }
        with request_context("POST", "/conflicts/resolve", payload=resolution):
            status, resolved = unpack(await self.object_handler.resolve_conflict())
        self.assertEqual(status, 200, resolved)
        self.assertEqual(resolved["data"]["conflict"]["status"], "dismissed")
        self.assertEqual(resolved["data"]["conflict"]["resolved_by"], "operator")

        with request_context("POST", "/conflicts/resolve", payload=resolution):
            status, locked = unpack(await self.object_handler.resolve_conflict())
        self.assertEqual(status, 423)
        self.assertEqual(locked["data"]["code"], "MEMORY_RESOURCE_LOCKED")

    async def test_supersede_conflict_resolution_preserves_objects(self):
        conflict, left, right = await self.create_pair("supersede")
        with request_context(
            "POST",
            "/conflicts/resolve",
            payload={
                "owner_user_id": self.context.owner_user_id,
                "conflict_id": conflict.conflict_id,
                "action": "supersede_right",
                "expected_versions": {
                    left.memory_item_id: left.version,
                    right.memory_item_id: right.version,
                },
                "reason": "左侧证据更新",
            },
        ):
            status, resolved = unpack(await self.object_handler.resolve_conflict())
        self.assertEqual(status, 200, resolved)
        right_after = await self.store.get_item(
            owner_user_id=self.context.owner_user_id,
            memory_item_id=right.memory_item_id,
        )
        self.assertEqual(right_after.status.value, "superseded")
        revisions = await self.store.list_revisions(
            owner_user_id=self.context.owner_user_id,
            memory_item_id=right.memory_item_id,
        )
        self.assertEqual(revisions[0].operation.value, "supersede")

    async def test_conflict_resolution_rolls_back_object_mutations_when_record_update_fails(self):
        conflict, left, right = await self.create_pair("rollback")
        async with aiosqlite.connect(self.store.db_path) as db:
            await db.execute(
                """
                CREATE TRIGGER fail_conflict_resolution
                BEFORE UPDATE OF status ON memory_conflicts
                WHEN NEW.status != 'open'
                BEGIN
                    SELECT RAISE(ABORT, 'forced conflict resolution failure');
                END
                """
            )
            await db.commit()

        with request_context(
            "POST",
            "/conflicts/resolve",
            payload={
                "owner_user_id": self.context.owner_user_id,
                "conflict_id": conflict.conflict_id,
                "action": "merge",
                "expected_versions": {
                    left.memory_item_id: left.version,
                    right.memory_item_id: right.version,
                },
                "content": "事务必须整体回滚",
            },
        ):
            status, failed = unpack(await self.object_handler.resolve_conflict())
        self.assertEqual(status, 409)
        self.assertEqual(failed["data"]["code"], "MEMORY_CONSTRAINT_CONFLICT")

        conflict_after = await self.store.get_conflict(
            owner_user_id=self.context.owner_user_id,
            conflict_id=conflict.conflict_id,
        )
        left_after = await self.store.get_item(
            owner_user_id=self.context.owner_user_id,
            memory_item_id=left.memory_item_id,
        )
        right_after = await self.store.get_item(
            owner_user_id=self.context.owner_user_id,
            memory_item_id=right.memory_item_id,
        )
        self.assertEqual(conflict_after.status.value, "open")
        self.assertEqual((left_after.version, left_after.status.value), (left.version, left.status.value))
        self.assertEqual((right_after.version, right_after.status.value), (right.version, right.status.value))
        left_revisions = await self.store.list_revisions(
            owner_user_id=self.context.owner_user_id,
            memory_item_id=left.memory_item_id,
        )
        right_revisions = await self.store.list_revisions(
            owner_user_id=self.context.owner_user_id,
            memory_item_id=right.memory_item_id,
        )
        self.assertEqual(len(left_revisions), 1)
        self.assertEqual(len(right_revisions), 1)

    async def test_maintenance_aggregate_and_owner_scoped_retry(self):
        created = await self.manager.create(
            context=self.context,
            content="索引需要修复",
            operation_key="maintenance:create",
            expected_version=0,
            scope=MemoryScope.USER,
            actor_type=MemoryActorType.ADMIN,
            actor_id="operator",
            source={
                "source_key": "maintenance-source",
                "source_type": "manual",
                "availability": "partial",
            },
        )
        await self.store.set_index_status(
            owner_user_id=self.context.owner_user_id,
            memory_item_id=created.item.memory_item_id,
            status=IndexStatus.NEEDS_REPAIR,
            error="index offline",
        )
        with request_context("GET", "/maintenance/status"):
            status, maintenance = unpack(await self.maintenance_handler.get_maintenance_status())
        self.assertEqual(status, 200)
        self.assertEqual(set(maintenance["data"]), {"migration", "index", "sources"})
        self.assertTrue(
            {
                "state",
                "processed",
                "total",
                "created",
                "deduped",
                "skipped",
                "conflicted",
                "errors",
                "unresolved_owner_count",
            }.issubset(maintenance["data"]["migration"])
        )
        self.assertTrue(
            {
                "state",
                "synced_count",
                "pending_count",
                "needs_repair_count",
                "disabled_count",
                "last_success_at",
                "last_error",
            }.issubset(maintenance["data"]["index"])
        )
        self.assertTrue(
            {
                "total_items",
                "covered_items",
                "partial_items",
                "unavailable_items",
                "coverage_ratio",
            }.issubset(maintenance["data"]["sources"])
        )
        self.assertGreaterEqual(maintenance["data"]["index"]["needs_repair_count"], 1)
        self.assertEqual(maintenance["data"]["index"]["last_error"], "index offline")
        self.assertGreaterEqual(maintenance["data"]["sources"]["partial_items"], 1)

        with request_context(
            "POST",
            "/maintenance/index/retry",
            payload={
                "owner_user_id": self.context.owner_user_id,
                "items": [
                    {
                        "memory_item_id": created.item.memory_item_id,
                        "expected_version": created.item.version,
                    }
                ],
            },
        ):
            status, retried = unpack(await self.maintenance_handler.retry_index())
        self.assertEqual(status, 200, retried)
        self.assertEqual(retried["data"]["queued_count"], 1)
        current = await self.store.get_item(
            owner_user_id=self.context.owner_user_id,
            memory_item_id=created.item.memory_item_id,
        )
        self.assertEqual(current.index_status.value, "pending")


if __name__ == "__main__":
    unittest.main()
