from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiosqlite

ASTRBOT_ROOT = Path(__file__).resolve().parents[4]
if str(ASTRBOT_ROOT) not in sys.path:
    sys.path.insert(0, str(ASTRBOT_ROOT))

from data.plugins.astrbot_zhouyi_plugin.memory.core.event_handler_modules.memory_reflection import (
    MemoryReflection,
    build_summary_identity,
    persist_summary_key_facts,
    retry_pending_summary_key_facts,
)
from data.plugins.astrbot_zhouyi_plugin.memory.core.i18n_backend import init
from data.plugins.astrbot_zhouyi_plugin.memory.core.managers.evolving_memory_manager import (
    EvolvingMemoryManager,
)
from data.plugins.astrbot_zhouyi_plugin.memory.core.memory_commands import MemoryCommands
from data.plugins.astrbot_zhouyi_plugin.memory.storage.evolving_memory_store import (
    EvolvingMemoryStore,
)


class _Event:
    unified_msg_origin = "qq:FriendMessage:10001"

    def get_platform_name(self):
        return "qq"

    def get_self_id(self):
        return "bot-a"

    def get_sender_id(self):
        return "10001"

    def plain_result(self, text):
        return text


class SummaryKeyFactsDualWriteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        temp_root = Path(__file__).resolve().parents[1] / "temp"
        temp_root.mkdir(parents=True, exist_ok=True)
        self.temp_dir = tempfile.TemporaryDirectory(dir=temp_root)
        self.db_path = str(Path(self.temp_dir.name) / "summary-feedback.db")
        self.store = EvolvingMemoryStore(self.db_path)
        self.manager = EvolvingMemoryManager(self.store)
        await self.manager.initialize()
        self.engine = SimpleNamespace(evolving_memory_manager=self.manager)
        self.messages = [
            SimpleNamespace(
                id=11,
                role="user",
                sender_id="10001",
                platform="qq",
                group_id=None,
                content="我喜欢草莓蛋糕",
            ),
            SimpleNamespace(
                id=12,
                role="assistant",
                sender_id="bot-a",
                platform="qq",
                group_id=None,
                content="我记住了",
            ),
        ]
        self.identity = build_summary_identity(
            _Event(),
            session_id=_Event.unified_msg_origin,
            is_group=False,
            history_messages=self.messages,
        )

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_key_facts_are_owner_scoped_traceable_and_idempotent(self):
        metadata = {
            "key_facts": ["用户喜欢草莓蛋糕", "用户周末学习 Python"],
            "canonical_summary": "用户说明了甜点偏好和学习计划",
            "confidence": 0.8,
        }
        first = await persist_summary_key_facts(
            memory_engine=self.engine,
            identity=self.identity,
            history_messages=self.messages,
            persona_id="persona-a",
            metadata=metadata,
            legacy_document_id=41,
            importance=0.75,
            triggered_by="memory-reflection",
        )
        self.assertEqual(first["created"], 2)
        self.assertEqual(first["failed"], 0)

        context = await self.manager.build_access_context(
            platform_id="qq",
            bot_id="bot-a",
            external_user_id="10001",
            session_id=_Event.unified_msg_origin,
            persona_id="persona-a",
            is_group=False,
        )
        items = await self.store.list_items(context=context)
        self.assertEqual(len(items), 2)
        for item in items:
            sources = await self.store.list_sources(
                owner_user_id=context.owner_user_id,
                memory_item_id=item.memory_item_id,
            )
            self.assertEqual(len(sources), 1)
            self.assertEqual(sources[0].document_id, 41)
            self.assertEqual(sources[0].message_start_id, 11)
            self.assertEqual(sources[0].message_end_id, 12)
            self.assertEqual(sources[0].source_type, "summary_key_fact")

        replay = await persist_summary_key_facts(
            memory_engine=self.engine,
            identity=self.identity,
            history_messages=self.messages,
            persona_id="persona-a",
            metadata=metadata,
            legacy_document_id=41,
            importance=0.75,
            triggered_by="memory-reflection",
        )
        self.assertEqual(replay["deduplicated"], 2)
        self.assertEqual(len(await self.store.list_items(context=context)), 2)

    async def test_retry_queue_deduplicates_when_initial_backfill_wins(self):
        metadata = {
            "session_id": _Event.unified_msg_origin,
            "persona_id": "persona-a",
            "platform_id": "qq",
            "bot_id": "bot-a",
            "external_user_id": "10001",
            "key_facts": ["回填和运行期重试只保留一个对象"],
            "importance": 0.7,
            "source_window": {
                "message_start_id": 11,
                "message_end_id": 12,
                "sender_ids": ["10001"],
            },
        }
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "CREATE TABLE documents(id INTEGER PRIMARY KEY, text TEXT, metadata TEXT)"
            )
            await db.execute(
                "INSERT INTO documents(id, text, metadata) VALUES (?, ?, ?)",
                (91, "legacy summary", json.dumps(metadata, ensure_ascii=False)),
            )
            await db.commit()

        original_create = self.manager.create
        self.manager.create = AsyncMock(side_effect=RuntimeError("runtime failed"))
        try:
            failed = await persist_summary_key_facts(
                memory_engine=self.engine,
                identity=self.identity,
                history_messages=self.messages,
                persona_id="persona-a",
                metadata=metadata,
                legacy_document_id=91,
                importance=0.7,
                triggered_by="memory-reflection",
            )
        finally:
            self.manager.create = original_create

        self.assertEqual(failed["queued"], 1)
        backfill = await self.manager.backfill_legacy_key_facts()
        self.assertEqual(backfill["created"], 1)

        retried = await retry_pending_summary_key_facts(self.engine)
        self.assertEqual(retried["completed"], 1)
        self.assertEqual(retried["remaining"], 0)

        context = await self.manager.build_access_context(
            platform_id="qq",
            bot_id="bot-a",
            external_user_id="10001",
            session_id=_Event.unified_msg_origin,
            persona_id="persona-a",
            is_group=False,
        )
        self.assertEqual(len(await self.store.list_items(context=context)), 1)

    async def test_private_summary_without_persona_uses_user_scope(self):
        identity = dict(self.identity)
        identity["external_user_id"] = "10003"
        result = await persist_summary_key_facts(
            memory_engine=self.engine,
            identity=identity,
            history_messages=self.messages,
            persona_id=None,
            metadata={"key_facts": ["无人格时仍可对象化"]},
            legacy_document_id=49,
            importance=0.5,
            triggered_by="memory-reflection",
        )
        self.assertEqual(result["created"], 1)
        context = await self.manager.build_access_context(
            platform_id="qq",
            bot_id="bot-a",
            external_user_id="10003",
            session_id=_Event.unified_msg_origin,
            persona_id=None,
            is_group=False,
        )
        items = await self.store.list_items(context=context)
        self.assertEqual(items[0].scope.value, "user")

    async def test_missing_identity_or_message_ids_degrades_without_raising(self):
        metadata = {"key_facts": ["可保留的旧总结事实"]}
        missing_identity = await persist_summary_key_facts(
            memory_engine=self.engine,
            identity=None,
            history_messages=self.messages,
            persona_id=None,
            metadata=metadata,
            legacy_document_id=51,
            importance=0.5,
            triggered_by="manual-summary",
        )
        self.assertEqual(missing_identity["skipped"], 1)
        self.assertTrue(missing_identity["errors"])

        missing_ids = await persist_summary_key_facts(
            memory_engine=self.engine,
            identity=self.identity,
            history_messages=[SimpleNamespace(id=0)],
            persona_id=None,
            metadata=metadata,
            legacy_document_id=52,
            importance=0.5,
            triggered_by="manual-summary",
        )
        self.assertEqual(missing_ids["skipped"], 1)
        self.assertTrue(missing_ids["errors"])


class SummaryFlowIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_automatic_reflection_advances_after_evolving_write_failure(self):
        access_context = SimpleNamespace(
            owner_user_id="owner-a",
            session_id="qq:FriendMessage:10001",
            persona_id="persona-a",
            is_group=False,
        )
        evolving_manager = SimpleNamespace(
            evolving_config={"enabled": True, "write_enabled": True},
            build_access_context=AsyncMock(return_value=access_context),
            create=AsyncMock(
                side_effect=[
                    RuntimeError("evolving failed"),
                    SimpleNamespace(deduplicated=False),
                ]
            ),
        )
        memory_engine = SimpleNamespace(
            evolving_memory_manager=evolving_manager,
            add_memory=AsyncMock(return_value=61),
        )
        updates = []
        session_metadata = {}

        async def get_metadata(_session_id, key, default=None):
            return session_metadata.get(key, default)

        async def update_metadata(session_id, key, value):
            updates.append((session_id, key, value))
            session_metadata[key] = value

        conversation_manager = SimpleNamespace(
            get_session_metadata=get_metadata,
            update_session_metadata=update_metadata,
        )
        processor = SimpleNamespace(
            process_conversation=AsyncMock(
                return_value=(
                    "legacy summary",
                    {"key_facts": ["对象写入会失败"]},
                    0.7,
                )
            ),
            classify_atoms_from_metadata=lambda **_kwargs: [],
        )
        reflection = MemoryReflection(
            context=SimpleNamespace(),
            config_manager=SimpleNamespace(),
            memory_engine=memory_engine,
            memory_processor=processor,
            conversation_manager=conversation_manager,
            message_utils=SimpleNamespace(),
            storage_tasks=set(),
            storage_sessions_inflight=set(),
            storage_state_lock=__import__("asyncio").Lock(),
        )
        messages = [
            SimpleNamespace(
                id=21,
                role="user",
                sender_id="10001",
                platform="qq",
                group_id=None,
            ),
            SimpleNamespace(
                id=22,
                role="assistant",
                sender_id="bot-a",
                platform="qq",
                group_id=None,
            ),
        ]
        identity = {
            "platform_id": "qq",
            "bot_id": "bot-a",
            "external_user_id": "10001",
            "session_id": "qq:FriendMessage:10001",
            "is_group": False,
        }

        await reflection._storage_task(
            "qq:FriendMessage:10001",
            messages,
            "persona-a",
            0,
            2,
            0,
            identity,
        )

        self.assertEqual(
            len(memory_engine._summary_key_facts_retry_queue),
            1,
        )

        # 下一次自动总结循环先重放对象双写，再因窗口已推进而退出；
        # legacy 文档不会被重复写入。
        await reflection._storage_task(
            "qq:FriendMessage:10001",
            messages,
            "persona-a",
            0,
            2,
            0,
            identity,
        )

        memory_engine.add_memory.assert_awaited_once()
        self.assertEqual(evolving_manager.create.await_count, 2)
        self.assertFalse(memory_engine._summary_key_facts_retry_queue)
        self.assertIn(
            ("qq:FriendMessage:10001", "last_summarized_index", 2), updates
        )
        self.assertIn(("qq:FriendMessage:10001", "pending_summary", None), updates)

    async def test_manual_summary_reports_partial_evolving_write(self):
        init("zh")
        messages = [
            SimpleNamespace(
                id=31,
                role="user",
                sender_id="10001",
                platform="qq",
                group_id=None,
            ),
            SimpleNamespace(
                id=32,
                role="assistant",
                sender_id="bot-a",
                platform="qq",
                group_id=None,
            ),
        ]
        conversation_manager = SimpleNamespace(
            store=SimpleNamespace(get_message_count=AsyncMock(return_value=2)),
            get_session_metadata=AsyncMock(return_value=0),
            get_messages_range=AsyncMock(return_value=messages),
            update_session_metadata=AsyncMock(),
        )
        engine = SimpleNamespace(add_memory=AsyncMock(return_value=71))
        processor = SimpleNamespace(
            process_conversation=AsyncMock(
                return_value=(
                    "legacy summary",
                    {"key_facts": ["对象化失败"], "topics": ["测试"]},
                    0.6,
                )
            ),
            classify_atoms_from_metadata=lambda **_kwargs: [],
        )
        commands = MemoryCommands(
            context=SimpleNamespace(),
            config_manager=SimpleNamespace(),
            memory_engine=engine,
            conversation_manager=conversation_manager,
            index_validator=None,
            memory_processor=processor,
        )
        partial = {
            "attempted": 1,
            "created": 0,
            "deduplicated": 0,
            "failed": 1,
            "skipped": 0,
            "errors": ["forced"],
        }
        with (
            patch(
                "data.plugins.astrbot_zhouyi_plugin.memory.core.utils.get_persona_id",
                AsyncMock(return_value="persona-a"),
            ),
            patch(
                "data.plugins.astrbot_zhouyi_plugin.memory.core.memory_commands.persist_summary_key_facts",
                AsyncMock(return_value=partial),
            ),
        ):
            results = [result async for result in commands.handle_summarize(_Event())]

        self.assertEqual(len(results), 2)
        self.assertIn("旧版总结已保存", results[-1])
        conversation_manager.update_session_metadata.assert_any_await(
            _Event.unified_msg_origin, "last_summarized_index", 2
        )

    async def test_manual_summary_retries_failed_dual_write_without_duplicate_legacy(self):
        init("zh")
        messages = [
            SimpleNamespace(
                id=41,
                role="user",
                sender_id="10001",
                platform="qq",
                group_id=None,
            ),
            SimpleNamespace(
                id=42,
                role="assistant",
                sender_id="bot-a",
                platform="qq",
                group_id=None,
            ),
        ]
        session_metadata = {}

        async def get_metadata(_session_id, key, default=None):
            return session_metadata.get(key, default)

        async def update_metadata(_session_id, key, value):
            session_metadata[key] = value

        conversation_manager = SimpleNamespace(
            store=SimpleNamespace(get_message_count=AsyncMock(return_value=2)),
            get_session_metadata=get_metadata,
            get_messages_range=AsyncMock(return_value=messages),
            update_session_metadata=update_metadata,
        )
        access_context = SimpleNamespace(
            owner_user_id="owner-a",
            session_id=_Event.unified_msg_origin,
            persona_id="persona-a",
            is_group=False,
        )
        evolving_manager = SimpleNamespace(
            evolving_config={"enabled": True, "write_enabled": True},
            build_access_context=AsyncMock(return_value=access_context),
            create=AsyncMock(
                side_effect=[
                    RuntimeError("first dual-write failed"),
                    SimpleNamespace(deduplicated=False),
                ]
            ),
        )
        engine = SimpleNamespace(
            add_memory=AsyncMock(return_value=81),
            evolving_memory_manager=evolving_manager,
        )
        processor = SimpleNamespace(
            process_conversation=AsyncMock(
                return_value=(
                    "legacy summary",
                    {"key_facts": ["手工总结对象化可重试"], "topics": ["测试"]},
                    0.6,
                )
            ),
            classify_atoms_from_metadata=lambda **_kwargs: [],
        )
        commands = MemoryCommands(
            context=SimpleNamespace(),
            config_manager=SimpleNamespace(),
            memory_engine=engine,
            conversation_manager=conversation_manager,
            index_validator=None,
            memory_processor=processor,
        )

        with patch(
            "data.plugins.astrbot_zhouyi_plugin.memory.core.utils.get_persona_id",
            AsyncMock(return_value="persona-a"),
        ):
            first = [result async for result in commands.handle_summarize(_Event())]
            second = [result async for result in commands.handle_summarize(_Event())]

        self.assertIn("旧版总结已保存", first[-1])
        self.assertIn("没有需要总结的新对话", second[-1])
        engine.add_memory.assert_awaited_once()
        self.assertEqual(evolving_manager.create.await_count, 2)
        self.assertFalse(engine._summary_key_facts_retry_queue)


if __name__ == "__main__":
    unittest.main()
