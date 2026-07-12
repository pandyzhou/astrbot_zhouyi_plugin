import importlib.util
import json
import multiprocessing
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_PATH = Path(__file__).parents[1] / "memory" / "config_migration.py"
MODULE_SPEC = importlib.util.spec_from_file_location("livingmemory_config_migration", MODULE_PATH)
if MODULE_SPEC is None or MODULE_SPEC.loader is None:
    raise RuntimeError(f"无法加载配置迁移模块: {MODULE_PATH}")
config_migration = importlib.util.module_from_spec(MODULE_SPEC)
MODULE_SPEC.loader.exec_module(config_migration)

LEGACY_CONFIG_FILENAME = config_migration.LEGACY_CONFIG_FILENAME
TARGET_CONFIG_FILENAME = config_migration.TARGET_CONFIG_FILENAME
TARGET_BACKUP_SUFFIX = config_migration.TARGET_BACKUP_SUFFIX
get_config_paths = config_migration.get_config_paths
migrate_config = config_migration.migrate_config
migrate_config_file = config_migration.migrate_config_file
wrap_legacy_config = config_migration.wrap_legacy_config


def _paused_migration_worker(config_dir, compared, release, results) -> None:
    original_replace = config_migration._atomic_replace

    def paused_replace(path, content):
        if path.name == TARGET_CONFIG_FILENAME:
            compared.set()
            if not release.wait(5):
                raise RuntimeError("等待并发写入测试释放超时")
        original_replace(path, content)

    with patch.object(config_migration, "_atomic_replace", side_effect=paused_replace):
        results.put(("migration", migrate_config_file(config_dir)))


def _locked_writer_worker(config_dir, compared, attempting, results) -> None:
    if not compared.wait(5):
        raise RuntimeError("等待迁移进入写入阶段超时")
    target_path = Path(config_dir) / TARGET_CONFIG_FILENAME
    attempting.set()
    with config_migration._migration_lock(target_path):
        current = json.loads(target_path.read_text(encoding="utf-8"))
        current["concurrent"] = True
        config_migration._atomic_replace(target_path, config_migration._serialize(current))
    results.put(("writer", True))


class ConfigMigrationTests(unittest.TestCase):
    def test_pure_helpers_build_paths_and_preserve_unknown_fields(self) -> None:
        config_dir = Path("config")
        self.assertEqual(
            get_config_paths(config_dir),
            (
                config_dir / LEGACY_CONFIG_FILENAME,
                config_dir / TARGET_CONFIG_FILENAME,
            ),
        )

        legacy = {"enabled": False, "bot_language": "zh", "unknown": {"保留": True}}
        migrated = wrap_legacy_config(legacy)

        self.assertEqual(
            migrated,
            {
                "memory": {
                    "enabled": False,
                    "bot_language": "zh",
                    "unknown": {"保留": True},
                }
            },
        )
        self.assertFalse(legacy["enabled"])

        root = {
            "memory": {"enabled": False, "nested": {"current": 1}},
            "living_memory": {
                "enabled": True,
                "nested": {"current": 9, "old_root": 2},
            },
            "unknown_root": {"保留": True},
        }
        self.assertEqual(
            migrate_config(root, {"nested": {"legacy": 3}, "legacy_only": 4}),
            {
                "memory": {
                    "enabled": False,
                    "nested": {"current": 1, "old_root": 2, "legacy": 3},
                    "legacy_only": 4,
                },
                "unknown_root": {"保留": True},
            },
        )

    def test_no_legacy_file_does_nothing(self) -> None:
        with tempfile.TemporaryDirectory(dir="temp") as directory:
            config_dir = Path(directory)
            self.assertFalse(migrate_config_file(config_dir))
            self.assertFalse((config_dir / TARGET_CONFIG_FILENAME).exists())

    def test_migrates_config_and_keeps_legacy_file(self) -> None:
        with tempfile.TemporaryDirectory(dir="temp") as directory:
            config_dir = Path(directory)
            legacy_path = config_dir / LEGACY_CONFIG_FILENAME
            target_path = config_dir / TARGET_CONFIG_FILENAME
            original = {
                "bot_language": "zh",
                "provider_settings": {"llm_provider_id": "模型一"},
                "future_field": 42,
            }
            legacy_text = json.dumps(original, ensure_ascii=False)
            legacy_path.write_text(legacy_text, encoding="utf-8")

            self.assertTrue(migrate_config_file(config_dir))
            self.assertEqual(legacy_path.read_text(encoding="utf-8"), legacy_text)
            self.assertEqual(
                json.loads(target_path.read_text(encoding="utf-8")),
                {
                    "memory": {
                        "enabled": True,
                        **original,
                    }
                },
            )

    def test_reads_utf8_bom(self) -> None:
        with tempfile.TemporaryDirectory(dir="temp") as directory:
            config_dir = Path(directory)
            legacy_path = config_dir / LEGACY_CONFIG_FILENAME
            target_path = config_dir / TARGET_CONFIG_FILENAME
            legacy_path.write_bytes(
                b"\xef\xbb\xbf" + json.dumps({"bot_language": "en"}).encode("utf-8")
            )

            self.assertTrue(migrate_config_file(config_dir))
            self.assertEqual(
                json.loads(target_path.read_text(encoding="utf-8")),
                {"memory": {"enabled": True, "bot_language": "en"}},
            )

    def test_existing_target_is_merged_after_one_time_backup(self) -> None:
        with tempfile.TemporaryDirectory(dir="temp") as directory:
            config_dir = Path(directory)
            legacy_path = config_dir / LEGACY_CONFIG_FILENAME
            target_path = config_dir / TARGET_CONFIG_FILENAME
            backup_path = target_path.with_name(f"{target_path.name}{TARGET_BACKUP_SUFFIX}")
            legacy_path.write_text(
                '{"bot_language": "zh", "unknown_legacy": 7}',
                encoding="utf-8",
            )
            target_text = json.dumps(
                {
                    "memory": {"enabled": False},
                    "living_memory": {"bot_language": "ru", "old_root": True},
                    "existing": True,
                },
                ensure_ascii=False,
            ) + "\n"
            target_path.write_text(target_text, encoding="utf-8")

            self.assertTrue(migrate_config_file(config_dir))
            self.assertEqual(backup_path.read_text(encoding="utf-8"), target_text)
            self.assertEqual(
                json.loads(target_path.read_text(encoding="utf-8")),
                {
                    "memory": {
                        "enabled": False,
                        "bot_language": "ru",
                        "old_root": True,
                        "unknown_legacy": 7,
                    },
                    "existing": True,
                },
            )
            backup_before = backup_path.read_bytes()
            self.assertFalse(migrate_config_file(config_dir))
            self.assertEqual(backup_path.read_bytes(), backup_before)
            self.assertTrue(legacy_path.exists())

    @unittest.skipUnless(config_migration.fcntl is not None, "需要 POSIX fcntl")
    def test_cross_process_lock_covers_compare_through_replace(self) -> None:
        with tempfile.TemporaryDirectory(dir="temp") as directory:
            config_dir = Path(directory)
            legacy_path = config_dir / LEGACY_CONFIG_FILENAME
            target_path = config_dir / TARGET_CONFIG_FILENAME
            legacy_path.write_text('{"bot_language": "zh"}', encoding="utf-8")
            target_path.write_text('{"existing": true}\n', encoding="utf-8")

            context = multiprocessing.get_context("fork")
            compared = context.Event()
            attempting = context.Event()
            release = context.Event()
            results = context.Queue()
            migration = context.Process(
                target=_paused_migration_worker,
                args=(directory, compared, release, results),
            )
            writer = context.Process(
                target=_locked_writer_worker,
                args=(directory, compared, attempting, results),
            )
            migration.start()
            writer.start()
            self.assertTrue(compared.wait(5))
            self.assertTrue(attempting.wait(5))
            release.set()
            migration.join(5)
            writer.join(5)

            self.assertEqual(migration.exitcode, 0)
            self.assertEqual(writer.exitcode, 0)
            self.assertCountEqual(
                [results.get(timeout=1), results.get(timeout=1)],
                [("migration", True), ("writer", True)],
            )
            self.assertEqual(
                json.loads(target_path.read_text(encoding="utf-8")),
                {
                    "existing": True,
                    "memory": {"enabled": True, "bot_language": "zh"},
                    "concurrent": True,
                },
            )
            backup_path = target_path.with_name(f"{target_path.name}{TARGET_BACKUP_SUFFIX}")
            self.assertEqual(
                json.loads(backup_path.read_text(encoding="utf-8")),
                {"existing": True},
            )

    def test_migration_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory(dir="temp") as directory:
            config_dir = Path(directory)
            legacy_path = config_dir / LEGACY_CONFIG_FILENAME
            target_path = config_dir / TARGET_CONFIG_FILENAME
            legacy_path.write_text('{"bot_language": "ru"}', encoding="utf-8")

            self.assertTrue(migrate_config_file(config_dir))
            first_result = target_path.read_bytes()
            self.assertFalse(migrate_config_file(config_dir))
            self.assertEqual(target_path.read_bytes(), first_result)
            self.assertTrue(legacy_path.exists())

    def test_written_json_is_utf8_pretty_printed_and_newline_terminated(self) -> None:
        with tempfile.TemporaryDirectory(dir="temp") as directory:
            config_dir = Path(directory)
            legacy_path = config_dir / LEGACY_CONFIG_FILENAME
            target_path = config_dir / TARGET_CONFIG_FILENAME
            legacy_path.write_text('{"提示": "星光"}', encoding="utf-8")

            self.assertTrue(migrate_config_file(config_dir))
            result = target_path.read_text(encoding="utf-8")
            self.assertTrue(result.endswith("\n"))
            self.assertIn('\n    "memory": {', result)
            self.assertIn('\n        "enabled": true', result)
            self.assertIn('"提示": "星光"', result)
            self.assertNotIn("\\u", result)


if __name__ == "__main__":
    unittest.main()
