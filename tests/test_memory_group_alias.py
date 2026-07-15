from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

ASTRBOT_ROOT = Path(__file__).resolve().parents[4]
if str(ASTRBOT_ROOT) not in sys.path:
    sys.path.insert(0, str(ASTRBOT_ROOT))

from astrbot.api.platform import MessageType

from data.plugins.astrbot_zhouyi_plugin.memory.core.memory_events import MemoryEvents


class _AliasDb:
    def __init__(self, alias=None, *, read_error: Exception | None = None):
        self.alias = alias
        self.read_error = read_error
        self.get_calls = 0
        self.upserts = []

    async def get_umo_alias(self, umo):
        self.get_calls += 1
        if self.read_error is not None:
            raise self.read_error
        if self.alias is not None and self.alias.umo == umo:
            return self.alias
        return None

    async def upsert_umo_alias(self, **values):
        self.upserts.append(values)
        self.alias = SimpleNamespace(**values)
        return self.alias


class _Context:
    def __init__(self, db):
        self.db = db

    def get_db(self):
        return self.db


class _Event:
    def __init__(
        self,
        umo="qq:GroupMessage:100",
        *,
        group_name=None,
        get_group=None,
        sender_id="sender-new",
        bot=None,
        self_id="2521827745",
    ):
        self.unified_msg_origin = umo
        self.message_obj = SimpleNamespace(
            group=SimpleNamespace(group_name=group_name),
            self_id=self_id,
        )
        if bot is not None:
            self.bot = bot
        self._get_group = get_group or AsyncMock(return_value=None)
        self._sender_id = sender_id

    def get_message_type(self):
        return MessageType.GROUP_MESSAGE

    def get_sender_id(self):
        return self._sender_id

    async def get_group(self, group_id):
        return await self._get_group(group_id)


async def _wait_alias_tasks(events: MemoryEvents):
    while events._group_alias_tasks:
        await asyncio.gather(*list(events._group_alias_tasks.values()))


class MemoryGroupAliasTests(unittest.IsolatedAsyncioTestCase):
    def _events(self, db):
        events = object.__new__(MemoryEvents)
        events.context = _Context(db)
        events._shutting_down = False
        events._group_alias_tasks = {}
        events._group_alias_pending = {}
        events._group_alias_remote_attempted = set()
        events._group_alias_last_known = {}
        events.GROUP_ALIAS_LOOKUP_TIMEOUT_SECONDS = 0.02
        return events

    async def test_local_group_name_preserves_user_alias_and_creator_and_updates_on_change(self):
        existing = SimpleNamespace(
            umo="qq:GroupMessage:100",
            creator_sender_id="creator-old",
            auto_name="旧群名",
            user_alias="用户自定义名",
        )
        db = _AliasDb(existing)
        events = self._events(db)

        first = _Event(group_name="新群名")
        events._schedule_group_alias_sync(first)
        events._schedule_group_alias_sync(first)
        await _wait_alias_tasks(events)

        self.assertEqual(len(db.upserts), 1)
        self.assertEqual(
            db.upserts[0],
            {
                "umo": "qq:GroupMessage:100",
                "creator_sender_id": "creator-old",
                "auto_name": "新群名",
                "user_alias": "用户自定义名",
            },
        )

        events._schedule_group_alias_sync(_Event(group_name="新群名"))
        await _wait_alias_tasks(events)
        self.assertEqual(len(db.upserts), 1)

        events._schedule_group_alias_sync(_Event(group_name="再次改名"))
        await _wait_alias_tasks(events)
        self.assertEqual(len(db.upserts), 2)
        self.assertEqual(db.upserts[-1]["auto_name"], "再次改名")
        self.assertEqual(db.upserts[-1]["user_alias"], "用户自定义名")
        self.assertEqual(db.upserts[-1]["creator_sender_id"], "creator-old")

    async def test_onebot_group_info_is_preferred_and_saves_actual_group_name(self):
        db = _AliasDb()
        events = self._events(db)
        call_action = AsyncMock(return_value={"group_name": "和朋友的Minecraft"})
        get_group = AsyncMock(return_value=SimpleNamespace(group_name="通用接口群名"))
        event = _Event(
            group_name="N/A",
            get_group=get_group,
            bot=SimpleNamespace(call_action=call_action),
        )

        events._schedule_group_alias_sync(event)
        await _wait_alias_tasks(events)

        call_action.assert_awaited_once_with(
            "get_group_info",
            group_id=100,
            self_id="2521827745",
        )
        get_group.assert_not_awaited()
        self.assertEqual(len(db.upserts), 1)
        self.assertEqual(db.upserts[0]["auto_name"], "和朋友的Minecraft")
        self.assertEqual(db.upserts[0]["umo"], "qq:GroupMessage:100")

    async def test_remote_lookup_succeeds_once_and_keeps_full_umo(self):
        db = _AliasDb()
        events = self._events(db)
        get_group = AsyncMock(return_value=SimpleNamespace(group_name="远程群名"))

        events._schedule_group_alias_sync(
            _Event("qq:GroupMessage:100:branch", group_name="unknown", get_group=get_group)
        )
        await _wait_alias_tasks(events)
        events._schedule_group_alias_sync(
            _Event("qq:GroupMessage:100:branch", group_name=None, get_group=get_group)
        )
        await _wait_alias_tasks(events)

        get_group.assert_awaited_once_with("100:branch")
        self.assertEqual(len(db.upserts), 1)
        self.assertEqual(db.upserts[0]["umo"], "qq:GroupMessage:100:branch")
        self.assertEqual(db.upserts[0]["auto_name"], "远程群名")

    async def test_remote_failure_and_timeout_only_attempt_once(self):
        async def wait_forever(_group_id):
            await asyncio.Event().wait()

        for get_group in (
            AsyncMock(side_effect=RuntimeError("remote failed")),
            AsyncMock(side_effect=wait_forever),
        ):
            with self.subTest(get_group=get_group):
                db = _AliasDb()
                events = self._events(db)
                event = _Event(group_name="N/A", get_group=get_group)

                events._schedule_group_alias_sync(event)
                await asyncio.wait_for(_wait_alias_tasks(events), timeout=0.2)
                events._schedule_group_alias_sync(_Event(group_name=None, get_group=get_group))
                await _wait_alias_tasks(events)

                get_group.assert_awaited_once_with("100")
                self.assertFalse(db.upserts)

    async def test_alias_errors_do_not_block_group_message_processing(self):
        db = _AliasDb(read_error=RuntimeError("db failed"))
        events = self._events(db)
        events._group_capture = SimpleNamespace(handle_all_group_messages=AsyncMock())
        event = _Event(group_name="可用群名")

        await asyncio.wait_for(events.handle_all_group_messages(event), timeout=0.05)
        events._group_capture.handle_all_group_messages.assert_awaited_once_with(event)
        await _wait_alias_tasks(events)
        self.assertFalse(db.upserts)

    async def test_remote_timeout_does_not_block_main_group_handler_and_shutdown_cleans_task(self):
        db = _AliasDb()
        events = self._events(db)
        events.GROUP_ALIAS_LOOKUP_TIMEOUT_SECONDS = 10
        events.STORAGE_SHUTDOWN_TIMEOUT_SECONDS = 0.01
        events._group_capture = SimpleNamespace(handle_all_group_messages=AsyncMock())
        events._memory_reflection = SimpleNamespace(set_shutting_down=lambda _value: None)
        events._storage_tasks = set()
        events._storage_sessions_inflight = set()
        async def wait_forever(_group_id):
            await asyncio.Event().wait()

        get_group = AsyncMock(side_effect=wait_forever)
        event = _Event(group_name=None, get_group=get_group)

        await asyncio.wait_for(events.handle_all_group_messages(event), timeout=0.05)
        self.assertTrue(events._group_alias_tasks)
        await asyncio.wait_for(events.shutdown(), timeout=0.2)

        self.assertFalse(events._group_alias_tasks)
        self.assertFalse(events._group_alias_pending)
        get_group.assert_awaited_once_with("100")


if __name__ == "__main__":
    unittest.main()
