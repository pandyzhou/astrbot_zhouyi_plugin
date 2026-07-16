from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from astrbot.core.provider.provider import Provider

from data.plugins.astrbot_zhouyi_plugin.memory.core.base.exceptions import InitializationError
from data.plugins.astrbot_zhouyi_plugin.memory.core.memory_bootstrap import MemoryBootstrap
from data.plugins.astrbot_zhouyi_plugin.memory.core.event_handler_modules.memory_reflection import (
    MemoryReflection,
)
from data.plugins.astrbot_zhouyi_plugin.memory.core.memory_events import MemoryEvents
from data.plugins.astrbot_zhouyi_plugin.memory.core.models.evolving_memory import (
    MemoryAccessContext,
)


class _Config:
    session_manager = {}

    def get(self, key, default=None):
        if key == "graph_memory.enabled":
            return True
        return default


class MemoryLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_initialization_failure_immediately_cleans_created_databases(self):
        order = []

        class FakeDatabase:
            def __init__(self, db_path, _index_path, _provider):
                self.name = "graph" if "graph" in db_path else "db"

            async def initialize(self):
                if self.name == "graph":
                    raise RuntimeError("graph initialize boom")

            async def close(self):
                order.append(self.name)

        with tempfile.TemporaryDirectory(dir="temp") as directory:
            bootstrap = MemoryBootstrap(object(), _Config(), directory)
            bootstrap.embedding_provider = object()
            bootstrap.llm_provider = Mock(spec=Provider)
            with patch.object(
                bootstrap,
                "_load_faiss_vec_db_class",
                return_value=FakeDatabase,
            ):
                with self.assertRaisesRegex(InitializationError, "graph initialize boom"):
                    await bootstrap._complete_initialization()

            self.assertEqual(order, ["graph", "db"])
            self.assertIsNone(bootstrap.db)
            self.assertIsNone(bootstrap.graph_db)
            self.assertIsNone(bootstrap.memory_engine)
            self.assertIsNone(bootstrap.conversation_store)
            self.assertIsNone(bootstrap.conversation_manager)
            self.assertIsNone(bootstrap.decay_scheduler)
            await bootstrap.cleanup_runtime_resources()
            self.assertEqual(order, ["graph", "db"])

    async def test_auto_migrate_false_keeps_v8_schema_and_disables_object_layer(self):
        class Context:
            def get_all_embedding_providers(self):
                return []

            def get_all_providers(self):
                return []

        class Config:
            session_manager = {}

            def get(self, key, default=None):
                values = {
                    "graph_memory.enabled": False,
                    "importance_decay.decay_rate": 0.0,
                    "forgetting_agent.auto_cleanup_enabled": False,
                    "migration_settings.auto_migrate": False,
                }
                return values.get(key, default)

        with tempfile.TemporaryDirectory(dir="temp") as directory:
            db_path = Path(directory) / "livingmemory.db"
            with sqlite3.connect(db_path) as db:
                db.execute(
                    """
                    CREATE TABLE db_version(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        version INTEGER NOT NULL,
                        description TEXT,
                        migrated_at TEXT NOT NULL,
                        migration_duration_seconds REAL
                    )
                    """
                )
                db.execute(
                    "INSERT INTO db_version(version, description, migrated_at) VALUES (8, 'v8', 'now')"
                )
                db.execute(
                    """
                    CREATE TABLE documents(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        doc_id TEXT,
                        text TEXT NOT NULL,
                        metadata TEXT DEFAULT '{}',
                        created_at TEXT,
                        updated_at TEXT
                    )
                    """
                )
                db.commit()

            bootstrap = MemoryBootstrap(Context(), Config(), directory)
            with patch(
                "data.plugins.astrbot_zhouyi_plugin.memory.core.memory_bootstrap.EvolvingMemoryStore.initialize",
                new=AsyncMock(side_effect=AssertionError("store must not run DDL")),
            ):
                self.assertTrue(await bootstrap.initialize())

            self.assertIsNotNone(bootstrap.memory_engine)
            self.assertIsNotNone(bootstrap.memory_engine.bm25_retriever)
            self.assertIsNone(bootstrap.evolving_memory_store)
            self.assertIsNone(bootstrap.evolving_memory_manager)
            self.assertFalse(
                bootstrap.get_runtime_status()["evolving_memory_enabled"]
            )

            with sqlite3.connect(db_path) as db:
                version = db.execute(
                    "SELECT version FROM db_version ORDER BY id DESC LIMIT 1"
                ).fetchone()[0]
                objects = {
                    row[0]
                    for row in db.execute(
                        "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
                    ).fetchall()
                }
            self.assertEqual(version, 8)
            self.assertTrue(
                {
                    "memory_owners",
                    "memory_identity_links",
                    "memory_items",
                    "memory_item_revisions",
                    "memory_item_sources",
                    "memory_item_relations",
                    "memory_conflicts",
                    "livingmemory_memory_items_fts",
                }.isdisjoint(objects)
            )
            await bootstrap.cleanup_runtime_resources()

    async def test_schema_readiness_rejects_version_only_v9_without_writes(self):
        with tempfile.TemporaryDirectory(dir="temp") as directory:
            db_path = Path(directory) / "livingmemory.db"
            with sqlite3.connect(db_path) as db:
                db.execute(
                    """
                    CREATE TABLE db_version(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        version INTEGER NOT NULL,
                        description TEXT,
                        migrated_at TEXT NOT NULL,
                        migration_duration_seconds REAL
                    )
                    """
                )
                db.execute(
                    "INSERT INTO db_version(version, description, migrated_at) VALUES (9, 'v9-only', 'now')"
                )
                db.commit()
                before = db.execute(
                    "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
                ).fetchall()

            bootstrap = MemoryBootstrap(object(), _Config(), directory)
            state = await bootstrap._check_evolving_schema_readiness(db_path)

            with sqlite3.connect(db_path) as db:
                after = db.execute(
                    "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
                ).fetchall()
            self.assertFalse(state["ready"])
            self.assertEqual(state["db_version"], 9)
            self.assertIn("memory_items", state["missing_objects"])
            self.assertEqual(after, before)

    async def test_cleanup_runtime_resources_is_reverse_order_and_idempotent(self):
        order = []

        def resource(name, method="close"):
            async def cleanup():
                order.append(name)

            return SimpleNamespace(**{method: cleanup})

        bootstrap = MemoryBootstrap(object(), _Config(), str(Path("temp") / "unused"))
        bootstrap.db = resource("db")
        bootstrap.graph_db = resource("graph_db")
        bootstrap.memory_engine = resource("memory_engine")
        bootstrap.conversation_store = resource("conversation_store")
        bootstrap.conversation_manager = SimpleNamespace(store=bootstrap.conversation_store)
        bootstrap.memory_processor = object()
        bootstrap.index_validator = object()
        bootstrap.db_migration = object()
        bootstrap.decay_scheduler = resource("scheduler", method="stop")
        bootstrap._initialization_complete = True

        await bootstrap.cleanup_runtime_resources()
        await bootstrap.cleanup_runtime_resources()

        self.assertEqual(
            order,
            ["scheduler", "conversation_store", "memory_engine", "graph_db", "db"],
        )
        for attribute in (
            "db",
            "graph_db",
            "memory_engine",
            "memory_processor",
            "conversation_store",
            "conversation_manager",
            "index_validator",
            "db_migration",
            "decay_scheduler",
        ):
            self.assertIsNone(getattr(bootstrap, attribute))
        self.assertFalse(bootstrap.is_initialized)

    async def test_tool_loop_final_response_is_recorded(self):
        conversation_manager = SimpleNamespace(
            add_message_from_event=AsyncMock(),
            get_session_info=AsyncMock(return_value=None),
        )
        reflection = MemoryReflection(
            context=SimpleNamespace(),
            config_manager=SimpleNamespace(get=lambda _key, default=None: default),
            memory_engine=SimpleNamespace(),
            memory_processor=SimpleNamespace(),
            conversation_manager=conversation_manager,
            message_utils=SimpleNamespace(enforce_message_limit=AsyncMock()),
            storage_tasks=set(),
            storage_sessions_inflight=set(),
            storage_state_lock=asyncio.Lock(),
        )
        event = SimpleNamespace(
            unified_msg_origin="qq:GroupMessage:20001",
            get_message_type=lambda: __import__(
                "astrbot.api.platform", fromlist=["MessageType"]
            ).MessageType.GROUP_MESSAGE,
        )
        response = SimpleNamespace(
            role="assistant",
            tools_call_name=None,
            tools_call_extra_content={"tool": "completed"},
            completion_text="这是工具调用后的最终可见回复",
        )

        await reflection.handle_memory_reflection(event, response)

        conversation_manager.add_message_from_event.assert_awaited_once_with(
            event=event,
            role="assistant",
            content="这是工具调用后的最终可见回复",
        )

    async def test_after_message_sent_only_resets_with_explicit_marker(self):
        events = object.__new__(MemoryEvents)
        events.handle_memory_feedback = AsyncMock()
        events._discard_feedback_buffer_for_event = AsyncMock()
        events.handle_session_reset = AsyncMock()
        extras = {}
        event = SimpleNamespace(get_extra=lambda key, default=None: extras.get(key, default))

        await events.handle_after_message_sent(event)
        events.handle_memory_feedback.assert_awaited_once_with(event)
        events.handle_session_reset.assert_not_awaited()

        events.handle_memory_feedback.reset_mock()
        extras["_clean_ltm_session"] = True
        await events.handle_after_message_sent(event)
        events._discard_feedback_buffer_for_event.assert_awaited_once_with(event)
        events.handle_session_reset.assert_awaited_once_with(event)
        events.handle_memory_feedback.assert_not_awaited()

    async def test_adaptive_feedback_triggers_on_trace_or_three_rounds(self):
        access_context = MemoryAccessContext(
            owner_user_id="owner-a",
            platform_id="qq",
            bot_id="bot-a",
            external_user_id="10001",
            session_id="qq:FriendMessage:10001",
            persona_id="persona-a",
            is_group=False,
        )
        manager = SimpleNamespace(
            evolving_config={
                "enabled": True,
                "feedback_enabled": True,
                "feedback_trigger_mode": "adaptive",
                "feedback_batch_rounds": 3,
                "feedback_idle_seconds": 300,
            }
        )
        events = object.__new__(MemoryEvents)
        events._shutting_down = False
        events.evolving_memory_manager = manager
        events.memory_processor = SimpleNamespace()
        events.context = SimpleNamespace()
        events._feedback_buffers = {}
        events._feedback_idle_tasks = {}
        events._feedback_tasks = set()
        events._feedback_inflight = set()
        events._feedback_lock = asyncio.Lock()
        events._schedule_feedback_flush = Mock()
        event = SimpleNamespace(
            unified_msg_origin=access_context.session_id,
            get_extra=lambda key, default=None: {
                "_livingmemory_response_context": {
                    "user_text": "普通闲聊",
                    "assistant_text": "普通回复",
                    "persona_id": "persona-a",
                    "recall_trace": [],
                }
            }.get(key, default),
        )

        with patch(
            "data.plugins.astrbot_zhouyi_plugin.memory.core.memory_events.build_access_context_from_event",
            AsyncMock(return_value=access_context),
        ):
            await events.handle_memory_feedback(event)
            await events.handle_memory_feedback(event)
            events._schedule_feedback_flush.assert_not_called()
            await events.handle_memory_feedback(event)
            events._schedule_feedback_flush.assert_called_once_with(
                (access_context.owner_user_id, access_context.session_id)
            )

            events._schedule_feedback_flush.reset_mock()
            events._feedback_buffers.clear()
            event.get_extra = lambda key, default=None: {
                "_livingmemory_response_context": {
                    "user_text": "普通闲聊",
                    "assistant_text": "召回后的回复",
                    "persona_id": "persona-a",
                    "recall_trace": [
                        {
                            "memory_item_id": "mem-a",
                            "version": 1,
                            "content": "事实",
                            "scope": "user",
                            "context": {"owner_user_id": "owner-a"},
                        }
                    ],
                }
            }.get(key, default)
            await events.handle_memory_feedback(event)
            events._schedule_feedback_flush.assert_called_once_with(
                (access_context.owner_user_id, access_context.session_id)
            )

        for task in list(events._feedback_idle_tasks.values()):
            task.cancel()
        await asyncio.gather(
            *list(events._feedback_idle_tasks.values()), return_exceptions=True
        )

    async def test_feedback_idle_timeout_schedules_owner_session_flush(self):
        key = ("owner-a", "qq:FriendMessage:10001")
        events = object.__new__(MemoryEvents)
        events._feedback_idle_tasks = {}
        events._schedule_feedback_flush = Mock()
        events._feedback_idle_tasks[key] = asyncio.current_task()

        await events._idle_feedback_flush(key, 0.001)

        events._schedule_feedback_flush.assert_called_once_with(key)
        self.assertNotIn(key, events._feedback_idle_tasks)

    async def test_feedback_failure_restores_owner_session_buffer(self):
        access_context = MemoryAccessContext(
            owner_user_id="owner-a",
            platform_id="qq",
            bot_id="bot-a",
            external_user_id="10001",
            session_id="qq:FriendMessage:10001",
            persona_id="persona-a",
            is_group=False,
        )
        events = object.__new__(MemoryEvents)
        events.evolving_memory_manager = SimpleNamespace(
            evolving_config={"feedback_idle_seconds": 300}
        )
        events.memory_processor = SimpleNamespace(
            evaluate_memory_feedback=AsyncMock(side_effect=RuntimeError("feedback boom"))
        )
        key = (access_context.owner_user_id, access_context.session_id)
        batch = [
            {
                "conversation": [
                    {"role": "user", "content": "记住我喜欢星星"},
                    {"role": "assistant", "content": "我记住了"},
                ],
                "recall_trace": [],
                "access_context": access_context,
            }
        ]
        events._feedback_buffers = {key: list(batch)}
        events._feedback_idle_tasks = {}
        events._feedback_tasks = set()
        events._feedback_inflight = {key}
        events._feedback_lock = asyncio.Lock()
        events._feedback_status = {
            "last_status": "idle",
            "last_error_hash": None,
            "completed_batches": 0,
            "failed_batches": 0,
        }
        events._shutting_down = False

        await events._flush_feedback_buffer(key)

        self.assertEqual(events._feedback_buffers[key], batch)
        self.assertEqual(events._feedback_status["failed_batches"], 1)
        idle_task = events._feedback_idle_tasks.pop(key)
        idle_task.cancel()
        await asyncio.gather(idle_task, return_exceptions=True)

    async def test_memory_events_shutdown_bounds_wait_and_cancels_storage_tasks(self):
        events = object.__new__(MemoryEvents)
        events._shutting_down = False
        events._memory_reflection = SimpleNamespace(set_shutting_down=lambda _value: None)
        events._storage_sessions_inflight = {"session"}
        events._storage_tasks = set()
        events._feedback_idle_tasks = {}
        events._feedback_buffers = {}
        events._feedback_tasks = set()
        events._feedback_inflight = set()
        events.STORAGE_SHUTDOWN_TIMEOUT_SECONDS = 0.01
        events.FEEDBACK_SHUTDOWN_TIMEOUT_SECONDS = 0.01

        completed = asyncio.create_task(asyncio.sleep(0))
        blocked = asyncio.create_task(asyncio.Event().wait())
        feedback_blocked = asyncio.create_task(asyncio.Event().wait())
        events._storage_tasks.update((completed, blocked))
        events._feedback_tasks.add(feedback_blocked)

        await asyncio.wait_for(events.shutdown(), timeout=0.2)

        self.assertTrue(events._shutting_down)
        self.assertTrue(blocked.cancelled())
        self.assertTrue(feedback_blocked.cancelled())
        self.assertFalse(events._storage_tasks)
        self.assertFalse(events._storage_sessions_inflight)


if __name__ == "__main__":
    unittest.main()
