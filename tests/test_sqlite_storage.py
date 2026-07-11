from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

ASTRBOT_ROOT = Path(__file__).resolve().parents[4]
if str(ASTRBOT_ROOT) not in sys.path:
    sys.path.insert(0, str(ASTRBOT_ROOT))

try:
    from astrbot.api import logger as _astrbot_logger  # noqa: F401
except ModuleNotFoundError:
    class _Logger:
        def info(self, *args, **kwargs):
            pass

        def warning(self, *args, **kwargs):
            pass

        def error(self, *args, **kwargs):
            pass

    astrbot_module = types.ModuleType("astrbot")
    api_module = types.ModuleType("astrbot.api")
    api_module.logger = _Logger()
    astrbot_module.api = api_module
    sys.modules["astrbot"] = astrbot_module
    sys.modules["astrbot.api"] = api_module

from data.plugins.astrbot_zhouyi_plugin.script.json_operate import (
    AUTO_CLEANUP_DAYS,
    DATABASE_NAME,
    MAX_HISTORY_POINTS,
    add_data,
    append_trend_point,
    auto_cleanup_servers,
    default_config,
    del_data,
    get_cleanup_candidates,
    get_group_storage,
    get_trend_history,
    initialize_storage,
    list_group_storages,
    read_json,
    update_server_status,
    write_json,
)


class SQLiteStorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        temp_root = Path(__file__).resolve().parents[1] / "temp"
        temp_root.mkdir(parents=True, exist_ok=True)
        self.temp_dir = tempfile.TemporaryDirectory(dir=temp_root)
        self.data_dir = Path(self.temp_dir.name)
        self.group_id = "12345"
        self.json_path = self.data_dir / f"{self.group_id}.json"
        self.storage = get_group_storage(self.data_dir, self.group_id)

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def _seed_server(self, group_id: str = "12345", *, last_success: int | None = None):
        storage = get_group_storage(self.data_dir, group_id)
        data = default_config()
        now = int(time.time())
        data["next_id"] = 2
        data["servers"] = {
            "1": {
                "id": 1,
                "name": f"Server-{group_id}",
                "host": f"{group_id}.example:25565",
                "created_time": now - 100,
                "last_success_time": now if last_success is None else last_success,
                "last_failed_time": None,
                "failed_count": 0,
            }
        }
        await write_json(storage, data)
        return storage

    def test_group_storage_validation_and_path(self):
        storage = get_group_storage(self.data_dir, "group_1-test")
        self.assertEqual(storage.db_path, (self.data_dir / DATABASE_NAME).resolve())
        self.assertEqual(storage.group_id, "group_1-test")
        for invalid in ("", "../x", "bad.name", "a" * 129):
            with self.subTest(group_id=invalid), self.assertRaises(ValueError):
                get_group_storage(self.data_dir, invalid)

    async def test_migration_preserves_all_fields_and_original_json(self):
        bucket = int(time.time()) // 3600 * 3600
        legacy = {
            "version": "2.2",
            "next_id": 8,
            "last_cleanup": bucket - 500,
            "servers": {
                "7": {
                    "id": 7,
                    "name": "Alpha",
                    "host": "alpha.example:25565",
                    "created_time": bucket - 400,
                    "last_success_time": bucket - 300,
                    "last_failed_time": bucket - 200,
                    "failed_count": 3,
                }
            },
            "trends": {"7": {"history": [{"ts": bucket - 3600, "count": 2}]}},
            "trend": {
                "server_id": "7",
                "history": [
                    {"ts": bucket + 123, "count": 4},
                    {"ts": bucket + 456, "count": 6},
                ],
            },
        }
        original = json.dumps(legacy, ensure_ascii=False)
        self.json_path.write_text(original, encoding="utf-8")

        storages = await initialize_storage(self.data_dir)
        self.assertEqual(storages, [self.storage])
        migrated = await read_json(self.storage)

        self.assertEqual(migrated["version"], "2.3")
        self.assertEqual(migrated["next_id"], 8)
        self.assertEqual(migrated["last_cleanup"], bucket - 500)
        self.assertEqual(
            migrated["servers"]["7"],
            {
                "id": 7,
                "name": "Alpha",
                "host": "alpha.example:25565",
                "created_time": bucket - 400,
                "last_success_time": bucket - 300,
                "last_failed_time": bucket - 200,
                "failed_count": 3,
            },
        )
        self.assertEqual(
            migrated["trends"]["7"]["history"],
            [
                {"ts": bucket - 3600, "count": 2},
                {"ts": bucket, "count": 6},
            ],
        )
        self.assertEqual(self.json_path.read_text(encoding="utf-8"), original)
        with closing(sqlite3.connect(self.storage.db_path)) as conn:
            status = conn.execute(
                "SELECT status FROM legacy_json_migrations WHERE group_id=?",
                (self.group_id,),
            ).fetchone()[0]
        self.assertEqual(status, "migrated")

    async def test_old_server_dictionary_migration_and_idempotence(self):
        legacy = {
            "first": {"name": "Alpha", "host": "alpha.example:25565"},
            "second": {"name": "Beta", "host": "beta.example:25565"},
        }
        self.json_path.write_text(json.dumps(legacy), encoding="utf-8")
        await initialize_storage(self.data_dir)
        first = await read_json(self.storage)
        self.assertEqual(first["next_id"], 3)
        self.assertEqual([item["name"] for item in first["servers"].values()], ["Alpha", "Beta"])

        self.assertTrue(await add_data(self.storage, "Database New", "new.example:25565"))
        self.json_path.write_text(
            json.dumps({"only": {"name": "Overwrite", "host": "overwrite.example"}}),
            encoding="utf-8",
        )
        listed = await list_group_storages(self.data_dir)
        self.assertEqual(listed, [self.storage])
        second = await read_json(self.storage)
        self.assertIn("Database New", {item["name"] for item in second["servers"].values()})
        self.assertNotIn("Overwrite", {item["name"] for item in second["servers"].values()})

        self.json_path.write_text("{broken", encoding="utf-8")
        with mock.patch.object(Path, "read_text", side_effect=AssertionError("legacy JSON was reread")):
            initialized = await initialize_storage(self.data_dir)
        self.assertEqual(initialized, [self.storage])
        self.assertEqual(await read_json(self.storage), second)
        self.assertEqual(self.json_path.read_text(encoding="utf-8"), "{broken")

    async def test_corrupt_json_does_not_block_other_groups(self):
        self.json_path.write_text("{broken", encoding="utf-8")
        valid_path = self.data_dir / "67890.json"
        valid = default_config()
        valid["servers"] = {
            "1": {"id": 1, "name": "Valid", "host": "valid.example:25565"}
        }
        valid["next_id"] = 2
        valid_path.write_text(json.dumps(valid), encoding="utf-8")

        groups = await initialize_storage(self.data_dir)
        self.assertEqual([item.group_id for item in groups], ["67890"])
        self.assertEqual(self.json_path.read_text(encoding="utf-8"), "{broken")
        self.assertIn("1", (await read_json(get_group_storage(self.data_dir, "67890")))["servers"])

    async def test_same_hour_upsert_negative_count_and_group_isolation(self):
        storage_a = await self._seed_server("100")
        storage_b = await self._seed_server("200")
        bucket = int(time.time()) // 3600 * 3600
        self.assertTrue(await append_trend_point(storage_a, "1", bucket + 10, 5))
        self.assertTrue(await append_trend_point(storage_a, "1", bucket + 3500, -9))
        self.assertTrue(await append_trend_point(storage_b, "1", bucket + 20, 8))

        self.assertEqual(await get_trend_history(storage_a, "1", 0), [{"ts": bucket, "count": 0}])
        self.assertEqual(await get_trend_history(storage_b, "1", 0), [{"ts": bucket, "count": 8}])
        self.assertNotEqual(
            (await read_json(storage_a))["servers"]["1"]["name"],
            (await read_json(storage_b))["servers"]["1"]["name"],
        )

    async def test_retention_keeps_latest_10000_in_ascending_order(self):
        await self._seed_server()
        with closing(sqlite3.connect(self.storage.db_path)) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executemany(
                "INSERT INTO trend_points(group_id, server_id, ts, count) VALUES(?, '1', ?, ?)",
                [(self.group_id, index * 3600, index) for index in range(1, MAX_HISTORY_POINTS + 1)],
            )
            conn.commit()
        newest_ts = (MAX_HISTORY_POINTS + 1) * 3600
        self.assertTrue(await append_trend_point(self.storage, "1", newest_ts + 10, 10001))
        history = await get_trend_history(self.storage, "1", 0)
        self.assertEqual(len(history), MAX_HISTORY_POINTS)
        self.assertEqual(history[0]["ts"], 2 * 3600)
        self.assertEqual(history[-1], {"ts": newest_ts, "count": 10001})
        self.assertEqual([item["ts"] for item in history], sorted(item["ts"] for item in history))

    async def test_delete_cascades_trends(self):
        await self._seed_server()
        now = int(time.time())
        await append_trend_point(self.storage, "1", now, 3)
        self.assertTrue(await del_data(self.storage, "Server-12345"))
        data = await read_json(self.storage)
        self.assertNotIn("1", data["servers"])
        self.assertNotIn("1", data["trends"])
        with closing(sqlite3.connect(self.storage.db_path)) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM trend_points WHERE group_id=?", (self.group_id,)
            ).fetchone()[0]
        self.assertEqual(count, 0)

    async def test_auto_cleanup_uses_max_success_and_latest_trend(self):
        now = int(time.time())
        old = now - (AUTO_CLEANUP_DAYS + 2) * 86400
        recent = now - 3600
        data = default_config()
        data["next_id"] = 4
        data["servers"] = {
            "1": {"id": 1, "name": "Old", "host": "old.example", "last_success_time": old},
            "2": {"id": 2, "name": "RecentTrend", "host": "trend.example", "last_success_time": old},
            "3": {"id": 3, "name": "RecentSuccess", "host": "success.example", "last_success_time": recent},
        }
        data["trends"] = {
            "1": {"history": [{"ts": old, "count": 1}]},
            "2": {"history": [{"ts": recent, "count": 2}]},
            "3": {"history": [{"ts": old, "count": 3}]},
        }
        await write_json(self.storage, data)

        candidates = await get_cleanup_candidates(self.storage)
        self.assertEqual([item["id"] for item in candidates], ["1"])
        before = (await read_json(self.storage))["last_cleanup"]
        deleted = await auto_cleanup_servers(self.storage)
        after = await read_json(self.storage)
        self.assertEqual([item["id"] for item in deleted], ["1"])
        self.assertNotIn("1", after["servers"])
        self.assertIn("2", after["servers"])
        self.assertIn("3", after["servers"])
        self.assertIsNone(before)
        self.assertIsNotNone(after["last_cleanup"])
        cleanup_time = after["last_cleanup"]
        self.assertEqual(await auto_cleanup_servers(self.storage), [])
        self.assertEqual((await read_json(self.storage))["last_cleanup"], cleanup_time)

    async def test_concurrent_writes_have_no_locked_errors_or_lost_updates(self):
        await initialize_storage(self.data_dir)
        additions = await asyncio.gather(
            *[
                add_data(self.storage, f"Server-{index}", f"host-{index}.example:25565")
                for index in range(40)
            ]
        )
        self.assertTrue(all(additions))
        data = await read_json(self.storage)
        self.assertEqual(len(data["servers"]), 40)
        self.assertEqual(len({item["id"] for item in data["servers"].values()}), 40)

        failures = await asyncio.gather(
            *[update_server_status(self.storage, "1", False) for _ in range(60)]
        )
        self.assertTrue(all(failures))
        self.assertEqual((await read_json(self.storage))["servers"]["1"]["failed_count"], 60)


if __name__ == "__main__":
    unittest.main()
