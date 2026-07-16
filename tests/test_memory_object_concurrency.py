from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from starlette.requests import Request

ASTRBOT_ROOT = Path(__file__).resolve().parents[4]
if str(ASTRBOT_ROOT) not in sys.path:
    sys.path.insert(0, str(ASTRBOT_ROOT))

from astrbot.api.web import PluginRequest, bind_request_context
from data.plugins.astrbot_zhouyi_plugin.memory.core.managers.evolving_memory_manager import EvolvingMemoryManager
from data.plugins.astrbot_zhouyi_plugin.memory.core.models.evolving_memory import MemoryActorType, MemoryScope
from data.plugins.astrbot_zhouyi_plugin.memory.core.page_api_modules import MemoryObjectHandler, PageApiUtils
from data.plugins.astrbot_zhouyi_plugin.memory.storage.evolving_memory_store import EvolvingMemoryStore


@contextmanager
def post_context(payload: dict, username: str | None = "operator"):
    body = json.dumps(payload).encode()
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
            "method": "POST",
            "scheme": "http",
            "path": "/page/v1/memory/objects/update",
            "raw_path": b"/page/v1/memory/objects/update",
            "query_string": b"",
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


class MemoryObjectConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp_root = Path(__file__).resolve().parents[1] / "temp"
        temp_root.mkdir(parents=True, exist_ok=True)
        self.temp_dir = tempfile.TemporaryDirectory(dir=temp_root)
        self.store = EvolvingMemoryStore(str(Path(self.temp_dir.name) / "concurrency.db"))
        self.manager = EvolvingMemoryManager(self.store)
        await self.manager.initialize()
        self.context = await self.manager.build_access_context(
            platform_id="qq",
            bot_id="main",
            external_user_id="20001",
            session_id="qq:FriendMessage:20001",
            persona_id="default",
            is_group=False,
        )
        created = await self.manager.create(
            context=self.context,
            content="初始内容",
            operation_key="test:create",
            expected_version=0,
            scope=MemoryScope.USER,
            actor_type=MemoryActorType.USER,
            actor_id="tester",
        )
        self.item = created.item
        service = SimpleNamespace(
            bootstrap=SimpleNamespace(
                evolving_memory_manager=self.manager,
                evolving_memory_store=self.store,
            )
        )
        self.handler = MemoryObjectHandler(PageApiUtils(), service)

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    async def update(self, content: str, *, username: str | None = "operator"):
        payload = {
            "owner_user_id": self.context.owner_user_id,
            "memory_item_id": self.item.memory_item_id,
            "expected_version": self.item.version,
            "content": content,
        }
        with post_context(payload, username=username):
            return unpack(await self.handler.update_object())

    async def test_parallel_updates_return_one_success_and_one_409(self):
        results = await asyncio.gather(self.update("写入 A"), self.update("写入 B"))
        self.assertEqual(sorted(status for status, _ in results), [200, 409])
        conflict = next(payload for status, payload in results if status == 409)
        self.assertEqual(conflict["data"]["code"], "MEMORY_REVISION_CONFLICT")
        self.assertEqual(conflict["data"]["details"]["expected_version"], 1)
        latest = await self.store.get_item(
            owner_user_id=self.context.owner_user_id,
            memory_item_id=self.item.memory_item_id,
        )
        self.assertEqual(latest.version, 2)
        self.assertIn(latest.content, {"写入 A", "写入 B"})

    async def test_mutation_without_dashboard_actor_returns_401(self):
        status, payload = await self.update("unauthorized", username=None)
        self.assertEqual(status, 401)
        self.assertEqual(payload["data"]["code"], "AUTH_REQUIRED")

    async def test_missing_evolving_runtime_returns_503(self):
        handler = MemoryObjectHandler(PageApiUtils(), SimpleNamespace(bootstrap=SimpleNamespace()))
        with post_context(
            {
                "owner_user_id": self.context.owner_user_id,
                "memory_item_id": self.item.memory_item_id,
                "expected_version": 1,
                "content": "x",
            }
        ):
            status, payload = unpack(await handler.update_object())
        self.assertEqual(status, 503)
        self.assertEqual(payload["data"]["code"], "EVOLVING_MEMORY_NOT_READY")


if __name__ == "__main__":
    unittest.main()
