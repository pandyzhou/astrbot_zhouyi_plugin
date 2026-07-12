from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from astrbot.core.provider.provider import Provider

from data.plugins.astrbot_zhouyi_plugin.memory.core.base.exceptions import InitializationError
from data.plugins.astrbot_zhouyi_plugin.memory.core.memory_bootstrap import MemoryBootstrap
from data.plugins.astrbot_zhouyi_plugin.memory.core.memory_events import MemoryEvents


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

    async def test_memory_events_shutdown_bounds_wait_and_cancels_storage_tasks(self):
        events = object.__new__(MemoryEvents)
        events._shutting_down = False
        events._memory_reflection = SimpleNamespace(set_shutting_down=lambda _value: None)
        events._storage_sessions_inflight = {"session"}
        events._storage_tasks = set()
        events.STORAGE_SHUTDOWN_TIMEOUT_SECONDS = 0.01

        completed = asyncio.create_task(asyncio.sleep(0))
        blocked = asyncio.create_task(asyncio.Event().wait())
        events._storage_tasks.update((completed, blocked))

        await asyncio.wait_for(events.shutdown(), timeout=0.2)

        self.assertTrue(events._shutting_down)
        self.assertTrue(blocked.cancelled())
        self.assertFalse(events._storage_tasks)
        self.assertFalse(events._storage_sessions_inflight)


if __name__ == "__main__":
    unittest.main()
