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

from data.plugins.astrbot_zhouyi_plugin.script import runtime_settings as runtime_settings_module
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
from data.plugins.astrbot_zhouyi_plugin.script.runtime_settings import (
    HistoryPruneConfirmationRequired,
    SettingsPreviewExpired,
    SettingsRevisionConflict,
    SettingsValidationError,
    apply_settings_update,
    get_effective_settings,
    get_global_settings,
    get_group_settings,
    preview_settings_update,
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

    async def test_schema_migrates_v1_to_v2_without_losing_data(self):
        with closing(sqlite3.connect(self.storage.db_path)) as conn:
            conn.executescript(
                """
                CREATE TABLE storage_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT INTO storage_meta VALUES('schema_version', '1');
                CREATE TABLE groups (
                    group_id TEXT PRIMARY KEY, next_id INTEGER NOT NULL DEFAULT 1, last_cleanup INTEGER
                );
                CREATE TABLE servers (
                    group_id TEXT NOT NULL, server_id TEXT NOT NULL, name TEXT NOT NULL, host TEXT NOT NULL,
                    created_time INTEGER, last_success_time INTEGER, last_failed_time INTEGER,
                    failed_count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(group_id, server_id), UNIQUE(group_id, name), UNIQUE(group_id, host),
                    FOREIGN KEY(group_id) REFERENCES groups(group_id) ON DELETE CASCADE
                );
                CREATE TABLE trend_points (
                    group_id TEXT NOT NULL, server_id TEXT NOT NULL, ts INTEGER NOT NULL, count INTEGER NOT NULL,
                    PRIMARY KEY(group_id, server_id, ts),
                    FOREIGN KEY(group_id, server_id) REFERENCES servers(group_id, server_id) ON DELETE CASCADE
                ) WITHOUT ROWID;
                CREATE TABLE legacy_json_migrations (
                    group_id TEXT PRIMARY KEY, source_path TEXT NOT NULL, status TEXT NOT NULL,
                    migrated_at INTEGER NOT NULL, message TEXT
                );
                INSERT INTO groups VALUES('12345', 2, 99);
                INSERT INTO servers(group_id, server_id, name, host) VALUES('12345', '1', 'Kept', 'kept.example');
                INSERT INTO trend_points VALUES('12345', '1', 3600, 7);
                """
            )
        await initialize_storage(self.data_dir)
        with closing(sqlite3.connect(self.storage.db_path)) as conn:
            self.assertEqual(
                conn.execute("SELECT value FROM storage_meta WHERE key='schema_version'").fetchone()[0],
                "2",
            )
            self.assertEqual(conn.execute("SELECT name FROM servers").fetchone()[0], "Kept")
            self.assertEqual(conn.execute("SELECT count FROM trend_points").fetchone()[0], 7)
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'runtime_%'"
                )
            }
        self.assertIn("runtime_global_settings", tables)
        self.assertIn("runtime_group_settings", tables)

    async def test_higher_schema_version_is_rejected(self):
        with closing(sqlite3.connect(self.storage.db_path)) as conn:
            conn.execute("CREATE TABLE storage_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            conn.execute("INSERT INTO storage_meta VALUES('schema_version', '99')")
            conn.commit()
        with self.assertRaisesRegex(RuntimeError, "高于当前支持版本"):
            await initialize_storage(self.data_dir)
        with closing(sqlite3.connect(self.storage.db_path)) as conn:
            self.assertEqual(
                conn.execute("SELECT value FROM storage_meta WHERE key='schema_version'").fetchone()[0],
                "99",
            )

    async def test_runtime_settings_defaults_override_reset_and_plain_io_isolation(self):
        await self._seed_server()
        global_settings = await get_global_settings(self.storage)
        self.assertEqual(global_settings.max_history_points, 10000)
        self.assertEqual(global_settings.max_concurrent_queries, 5)
        self.assertTrue(global_settings.trend_sampling_enabled)
        group_settings = await get_group_settings(self.storage)
        self.assertEqual(group_settings.revision, 0)
        self.assertIsNone(group_settings.auto_cleanup_days)

        applied = await apply_settings_update(
            self.storage,
            {"auto_cleanup_days": 30, "mc_lookup_timeout_seconds": 4},
            expected_revision=0,
        )
        effective = await get_effective_settings(self.storage)
        self.assertEqual(applied.settings.revision, 1)
        self.assertEqual(effective.auto_cleanup_days, 30)
        self.assertEqual(effective.mc_lookup_timeout_seconds, 4.0)
        self.assertEqual(effective.max_concurrent_queries, 5)

        data = await read_json(self.storage)
        await write_json(self.storage, data)
        self.assertEqual((await get_group_settings(self.storage)).revision, 1)

        reset = await apply_settings_update(
            self.storage,
            {},
            expected_revision=1,
            reset_keys=("auto_cleanup_days",),
        )
        self.assertIsNone(reset.settings.auto_cleanup_days)
        self.assertEqual((await get_effective_settings(self.storage)).auto_cleanup_days, 10)
        with self.assertRaises(SettingsValidationError):
            await apply_settings_update(
                self.storage, {"max_concurrent_queries": 8}, expected_revision=2
            )
        with self.assertRaises(SettingsValidationError):
            await apply_settings_update(
                self.storage, {"auto_cleanup_days": True}, expected_revision=2
            )

    async def test_preview_confirm_trims_in_transaction_and_global_skips_overrides(self):
        inherited = await self._seed_server("100")
        overridden = await self._seed_server("200")
        with closing(sqlite3.connect(inherited.db_path)) as conn:
            conn.executemany(
                "INSERT INTO trend_points(group_id, server_id, ts, count) VALUES(?, '1', ?, ?)",
                [
                    (group_id, index * 3600, index)
                    for group_id in ("100", "200")
                    for index in range(1, 221)
                ],
            )
            conn.commit()

        group_preview = await preview_settings_update(
            overridden, {"max_history_points": 200}
        )
        await apply_settings_update(
            overridden,
            {"max_history_points": 200},
            expected_revision=0,
            preview_id=group_preview.preview_id,
            confirm_history_prune=True,
        )
        self.assertEqual(len(await get_trend_history(overridden, "1", 0)), 200)

        global_preview = await preview_settings_update(
            inherited, {"max_history_points": 168}, scope="global"
        )
        self.assertEqual(global_preview.affected_groups, ("100",))
        self.assertEqual(global_preview.history_points_to_prune, 52)
        with self.assertRaises(HistoryPruneConfirmationRequired):
            await apply_settings_update(
                inherited,
                {"max_history_points": 168},
                expected_revision=1,
                scope="global",
                preview_id=global_preview.preview_id,
            )
        result = await apply_settings_update(
            inherited,
            {"max_history_points": 168},
            expected_revision=1,
            scope="global",
            preview_id=global_preview.preview_id,
            confirm_history_prune=True,
        )
        self.assertEqual(result.pruned_history_points, 52)
        self.assertEqual(len(await get_trend_history(inherited, "1", 0)), 168)
        self.assertEqual(len(await get_trend_history(overridden, "1", 0)), 200)

    async def test_zero_delete_limit_preview_saves_without_confirmation(self):
        await self._seed_server()
        preview = await preview_settings_update(
            self.storage, {"max_history_points": 168}
        )
        self.assertEqual(preview.history_points_to_prune, 0)

        result = await apply_settings_update(
            self.storage,
            {"max_history_points": 168},
            expected_revision=0,
            preview_id=preview.preview_id,
        )

        self.assertEqual(result.pruned_history_points, 0)
        self.assertEqual((await get_effective_settings(self.storage)).max_history_points, 168)

    async def test_group_preview_expires_when_global_revision_changes(self):
        await self._seed_server()
        preview = await preview_settings_update(
            self.storage, {"default_trend_hours": 48}
        )
        await apply_settings_update(
            self.storage,
            {"max_concurrent_queries": 6},
            expected_revision=1,
            scope="global",
        )

        with self.assertRaises(SettingsPreviewExpired):
            await apply_settings_update(
                self.storage,
                {"default_trend_hours": 48},
                expected_revision=0,
                preview_id=preview.preview_id,
            )

    async def test_global_preview_history_groups_exclude_cleanup_only_groups(self):
        history_group = await self._seed_server("100")
        cleanup_group = await self._seed_server("200")
        await apply_settings_update(
            history_group,
            {"auto_cleanup_days": 20},
            expected_revision=0,
        )
        cleanup_preview = await preview_settings_update(
            cleanup_group, {"max_history_points": 500}
        )
        await apply_settings_update(
            cleanup_group,
            {"max_history_points": 500},
            expected_revision=0,
            preview_id=cleanup_preview.preview_id,
        )

        preview = await preview_settings_update(
            history_group,
            {"max_history_points": 168, "auto_cleanup_days": 5},
            scope="global",
        )

        self.assertEqual(preview.affected_groups, ("100",))

    async def test_existing_v2_preview_table_adds_global_revision_column(self):
        await self._seed_server()
        with closing(sqlite3.connect(self.storage.db_path)) as conn:
            conn.execute("DROP TABLE runtime_settings_previews")
            conn.execute(
                """CREATE TABLE runtime_settings_previews (
                    preview_id TEXT PRIMARY KEY,
                    scope TEXT NOT NULL,
                    group_id TEXT,
                    base_revision INTEGER NOT NULL,
                    patch_json TEXT NOT NULL,
                    reset_keys_json TEXT NOT NULL,
                    history_points_to_prune INTEGER NOT NULL DEFAULT 0,
                    expires_at INTEGER NOT NULL
                )"""
            )
            conn.commit()

        await preview_settings_update(self.storage, {"default_trend_hours": 48})

        with closing(sqlite3.connect(self.storage.db_path)) as conn:
            columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(runtime_settings_previews)")
            }
        self.assertIn("global_revision", columns)

    async def test_revision_conflict_and_expired_preview_have_stable_codes(self):
        await self._seed_server()
        with self.assertRaises(SettingsRevisionConflict) as conflict:
            await apply_settings_update(
                self.storage, {"default_trend_hours": 48}, expected_revision=9
            )
        self.assertEqual(conflict.exception.code, "settings_revision_conflict")

        preview = await preview_settings_update(
            self.storage, {"max_history_points": 168}, preview_ttl_seconds=1
        )
        with mock.patch.object(
            runtime_settings_module.time, "time", return_value=preview.expires_at + 1
        ):
            with self.assertRaises(SettingsPreviewExpired) as expired:
                await apply_settings_update(
                    self.storage,
                    {"max_history_points": 168},
                    expected_revision=0,
                    preview_id=preview.preview_id,
                    confirm_history_prune=True,
                )
        self.assertEqual(expired.exception.code, "settings_preview_expired")

    async def test_history_prune_preview_rejects_changed_point_count(self):
        await self._seed_server()
        with closing(sqlite3.connect(self.storage.db_path)) as conn:
            conn.executemany(
                "INSERT INTO trend_points(group_id, server_id, ts, count) VALUES(?, '1', ?, ?)",
                [(self.group_id, index * 3600, index) for index in range(1, 171)],
            )
            conn.commit()

        preview = await preview_settings_update(
            self.storage, {"max_history_points": 168}
        )
        self.assertEqual(preview.history_points_to_prune, 2)
        with closing(sqlite3.connect(self.storage.db_path)) as conn:
            conn.execute(
                "INSERT INTO trend_points(group_id, server_id, ts, count) VALUES(?, '1', ?, ?)",
                (self.group_id, 171 * 3600, 171),
            )
            conn.commit()

        with self.assertRaises(SettingsPreviewExpired):
            await apply_settings_update(
                self.storage,
                {"max_history_points": 168},
                expected_revision=0,
                preview_id=preview.preview_id,
                confirm_history_prune=True,
                expected_history_points_to_prune=2,
            )
        self.assertEqual(len(await get_trend_history(self.storage, "1", 0)), 171)

    async def test_dynamic_retention_and_cleanup_preview_do_not_delete(self):
        now = int(time.time())
        old = now - 7 * 86400
        await self._seed_server(last_success=old)
        preview = await preview_settings_update(self.storage, {"auto_cleanup_days": 5})
        self.assertEqual(preview.cleanup_candidates_before, 0)
        self.assertEqual(preview.cleanup_candidates_after, 1)
        self.assertIn("1", (await read_json(self.storage))["servers"])
        await apply_settings_update(
            self.storage, {"auto_cleanup_days": 5}, expected_revision=0
        )
        self.assertEqual([item["id"] for item in await get_cleanup_candidates(self.storage)], ["1"])

        trend_preview = await preview_settings_update(
            self.storage, {"max_history_points": 168}
        )
        await apply_settings_update(
            self.storage,
            {"max_history_points": 168},
            expected_revision=1,
            preview_id=trend_preview.preview_id,
            confirm_history_prune=True,
        )
        with closing(sqlite3.connect(self.storage.db_path)) as conn:
            conn.executemany(
                "INSERT INTO trend_points(group_id, server_id, ts, count) VALUES(?, '1', ?, ?)",
                [(self.group_id, index * 3600, index) for index in range(1, 169)],
            )
            conn.commit()
        self.assertTrue(await append_trend_point(self.storage, "1", 169 * 3600, 169))
        history = await get_trend_history(self.storage, "1", 0)
        self.assertEqual(len(history), 168)
        self.assertEqual(history[0]["ts"], 2 * 3600)

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
