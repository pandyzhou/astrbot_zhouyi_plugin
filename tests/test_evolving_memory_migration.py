from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

import aiosqlite

ASTRBOT_ROOT = Path(__file__).resolve().parents[4]
if str(ASTRBOT_ROOT) not in sys.path:
    sys.path.insert(0, str(ASTRBOT_ROOT))

from data.plugins.astrbot_zhouyi_plugin.memory.core.managers.evolving_memory_manager import (
    EvolvingMemoryManager,
)
from data.plugins.astrbot_zhouyi_plugin.memory.data_migration import (
    _sqlite_counts_from_connection,
)
from data.plugins.astrbot_zhouyi_plugin.memory.core.models.evolving_memory import MemoryActorType
from data.plugins.astrbot_zhouyi_plugin.memory.storage.db_migration import DBMigration
from data.plugins.astrbot_zhouyi_plugin.memory.storage.evolving_memory_store import (
    EvolvingMemoryStore,
)


class EvolvingMemoryMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        temp_root = Path(__file__).resolve().parents[1] / "temp"
        temp_root.mkdir(parents=True, exist_ok=True)
        self.temp_dir = tempfile.TemporaryDirectory(dir=temp_root)
        self.db_path = Path(self.temp_dir.name) / "livingmemory.db"

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def _create_v8_database(self) -> dict[str, int]:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
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
            await db.execute(
                "INSERT INTO db_version(version, description, migrated_at, migration_duration_seconds) VALUES (8, 'v8', 'now', 0)"
            )
            await db.execute(
                "CREATE TABLE documents(id INTEGER PRIMARY KEY, text TEXT, metadata TEXT)"
            )
            await db.execute(
                "INSERT INTO documents(id, text, metadata) VALUES (1, '旧总结', '{}')"
            )
            await db.execute(
                """
                CREATE TABLE memory_atoms(
                    id INTEGER PRIMARY KEY,
                    parent_memory_id INTEGER NOT NULL,
                    content TEXT NOT NULL
                )
                """
            )
            await db.execute(
                "INSERT INTO memory_atoms(id, parent_memory_id, content) VALUES (1, 1, '旧原子')"
            )
            await db.execute(
                """
                CREATE TABLE graph_entries(
                    id INTEGER PRIMARY KEY,
                    entry_key TEXT NOT NULL UNIQUE,
                    source_memory_id INTEGER NOT NULL,
                    content TEXT NOT NULL
                )
                """
            )
            await db.execute(
                "INSERT INTO graph_entries(id, entry_key, source_memory_id, content) VALUES (1, 'g1', 1, '旧图条目')"
            )
            await db.execute(
                """
                CREATE TABLE memory_write_ops(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    op_type TEXT NOT NULL,
                    memory_id INTEGER,
                    status TEXT NOT NULL DEFAULT 'pending',
                    step TEXT NOT NULL DEFAULT 'started',
                    payload TEXT DEFAULT '{}',
                    error TEXT,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            await db.execute(
                "INSERT INTO memory_write_ops(op_type, status, step, created_at, updated_at) VALUES ('legacy', 'completed', 'done', 1, 1)"
            )
            await db.commit()
        return {"documents": 1, "memory_atoms": 1, "graph_entries": 1, "memory_write_ops": 1}

    async def test_v8_to_v9_is_idempotent_and_preserves_legacy_rows(self):
        before = await self._create_v8_database()
        migration = DBMigration(str(self.db_path))
        result = await migration.migrate()
        self.assertTrue(result["success"], result)
        self.assertEqual(await migration.get_db_version(), 9)

        async with aiosqlite.connect(self.db_path) as db:
            tables = {
                row[0]
                for row in await (
                    await db.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")
                ).fetchall()
            }
            for table in (
                "memory_owners",
                "memory_identity_links",
                "memory_items",
                "memory_item_revisions",
                "memory_item_sources",
                "memory_item_relations",
                "memory_conflicts",
                "livingmemory_memory_items_fts",
            ):
                self.assertIn(table, tables)
            for table, expected in before.items():
                row = await (await db.execute(f'SELECT COUNT(*) FROM "{table}"')).fetchone()
                self.assertGreaterEqual(int(row[0]), expected)

            atom_columns = {
                row[1] for row in await (await db.execute("PRAGMA table_info(memory_atoms)")).fetchall()
            }
            graph_columns = {
                row[1] for row in await (await db.execute("PRAGMA table_info(graph_entries)")).fetchall()
            }
            op_columns = {
                row[1] for row in await (await db.execute("PRAGMA table_info(memory_write_ops)")).fetchall()
            }
            self.assertTrue({"memory_item_id", "memory_revision_no"} <= atom_columns)
            self.assertTrue(
                {"memory_item_id", "memory_revision_no", "projection_status"}
                <= graph_columns
            )
            self.assertTrue({"operation_key", "entity_id"} <= op_columns)

        repeated = await migration.migrate()
        self.assertTrue(repeated["success"], repeated)
        self.assertEqual(repeated["from_version"], 9)
        self.assertEqual(repeated["to_version"], 9)

        await migration.ensure_v9_schema()
        async with aiosqlite.connect(self.db_path) as db:
            row = await (await db.execute("SELECT COUNT(*) FROM documents")).fetchone()
            self.assertEqual(int(row[0]), 1)

    async def test_v9_schema_transaction_rolls_back_on_failure(self):
        class FailingMigration(DBMigration):
            async def _apply_v9_schema(self, db):
                await super()._apply_v9_schema(db)
                raise RuntimeError("forced ddl failure")

        with self.assertRaisesRegex(RuntimeError, "forced ddl failure"):
            await FailingMigration(str(self.db_path)).ensure_v9_schema()
        async with aiosqlite.connect(self.db_path) as db:
            row = await (
                await db.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'memory_items'"
                )
            ).fetchone()
            self.assertEqual(int(row[0]), 0)

    async def test_v9_schema_is_safe_when_legacy_tables_do_not_exist(self):
        migration = DBMigration(str(self.db_path))
        await migration.ensure_v9_schema()
        await migration.ensure_v9_schema()
        async with aiosqlite.connect(self.db_path) as db:
            row = await (
                await db.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'memory_items'"
                )
            ).fetchone()
            self.assertEqual(int(row[0]), 1)

    async def test_deterministic_key_facts_backfill_and_checkpoint(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "CREATE TABLE documents(id INTEGER PRIMARY KEY, text TEXT, metadata TEXT)"
            )
            private_metadata = {
                "session_id": "qq:FriendMessage:10001",
                "persona_id": "persona-a",
                "platform_id": "qq",
                "bot_id": "bot-a",
                "sender_id": "10001",
                "key_facts": ["用户喜欢草莓蛋糕", "用户会编程"],
                "importance": 0.8,
                "source_window": {
                    "sender_id": "10001",
                    "message_start_id": 1,
                    "message_end_id": 4,
                },
            }
            group_metadata = {
                "session_id": "qq:GroupMessage:20001",
                "group_id": "20001",
                "key_facts": ["群里计划周末聚会"],
                "source_window": {"message_start_id": 5, "message_end_id": 8},
            }
            await db.execute(
                "INSERT INTO documents(id, text, metadata) VALUES (?, ?, ?)",
                (1, "私聊总结", json.dumps(private_metadata, ensure_ascii=False)),
            )
            await db.execute(
                "INSERT INTO documents(id, text, metadata) VALUES (?, ?, ?)",
                (2, "群聊总结", json.dumps(group_metadata, ensure_ascii=False)),
            )
            await db.commit()

        store = EvolvingMemoryStore(str(self.db_path))
        manager = EvolvingMemoryManager(store, evolving_memory={"migration_batch_size": 1})
        await manager.initialize()
        first = await manager.backfill_legacy_key_facts()
        self.assertEqual(first["created"], 3)
        self.assertEqual(first["cursor"], 2)

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    "SELECT memory_item_id, owner_user_id, scope, session_id FROM memory_items ORDER BY memory_item_id"
                )
            ).fetchall()
            first_ids = [str(row["memory_item_id"]) for row in rows]
            self.assertEqual(len(first_ids), 3)
            self.assertIn("legacy_session", {str(row["scope"]) for row in rows})
            self.assertIn("persona", {str(row["scope"]) for row in rows})
            await db.execute(
                "DELETE FROM migration_status WHERE key = 'evolving_memory_key_facts_v1'"
            )
            await db.commit()

        second = await manager.backfill_legacy_key_facts()
        self.assertEqual(second["created"], 0)
        self.assertEqual(second["dedup"], 3)
        async with aiosqlite.connect(self.db_path) as db:
            ids = [
                str(row[0])
                for row in await (
                    await db.execute(
                        "SELECT memory_item_id FROM memory_items ORDER BY memory_item_id"
                    )
                ).fetchall()
            ]
            self.assertEqual(ids, first_ids)
            documents = await (await db.execute("SELECT COUNT(*) FROM documents")).fetchone()
            self.assertEqual(int(documents[0]), 2)

    async def test_data_migration_core_counts_include_v9_tables(self):
        store = EvolvingMemoryStore(str(self.db_path))
        manager = EvolvingMemoryManager(store)
        await manager.initialize()
        context = await manager.build_access_context(
            platform_id="qq",
            bot_id="bot-a",
            external_user_id="10001",
            session_id="qq:FriendMessage:10001",
            persona_id="persona-a",
            is_group=False,
        )
        await manager.create(
            context=context,
            content="用于统计的事实",
            operation_key="create:count",
            actor_type=MemoryActorType.USER,
            actor_id="tester",
        )

        with sqlite3.connect(self.db_path) as db:
            counts = _sqlite_counts_from_connection(db)
        self.assertEqual(counts["memory_owners"], 1)
        self.assertEqual(counts["memory_identity_links"], 1)
        self.assertEqual(counts["memory_items"], 1)
        self.assertEqual(counts["memory_item_revisions"], 1)
        self.assertEqual(counts["memory_write_ops"], 1)


if __name__ == "__main__":
    unittest.main()
