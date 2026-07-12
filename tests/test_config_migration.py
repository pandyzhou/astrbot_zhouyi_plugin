import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_PATH = Path(__file__).parents[1] / "livingmemory" / "config_migration.py"
MODULE_SPEC = importlib.util.spec_from_file_location("livingmemory_config_migration", MODULE_PATH)
if MODULE_SPEC is None or MODULE_SPEC.loader is None:
    raise RuntimeError(f"无法加载配置迁移模块: {MODULE_PATH}")
config_migration = importlib.util.module_from_spec(MODULE_SPEC)
MODULE_SPEC.loader.exec_module(config_migration)

LEGACY_CONFIG_FILENAME = config_migration.LEGACY_CONFIG_FILENAME
TARGET_CONFIG_FILENAME = config_migration.TARGET_CONFIG_FILENAME
get_config_paths = config_migration.get_config_paths
migrate_config_file = config_migration.migrate_config_file
wrap_legacy_config = config_migration.wrap_legacy_config


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
                "living_memory": {
                    "enabled": True,
                    "bot_language": "zh",
                    "unknown": {"保留": True},
                }
            },
        )
        self.assertFalse(legacy["enabled"])

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
                    "living_memory": {
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
                {"living_memory": {"enabled": True, "bot_language": "en"}},
            )

    def test_existing_target_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory(dir="temp") as directory:
            config_dir = Path(directory)
            legacy_path = config_dir / LEGACY_CONFIG_FILENAME
            target_path = config_dir / TARGET_CONFIG_FILENAME
            legacy_path.write_text('{"bot_language": "zh"}', encoding="utf-8")
            target_text = '{"existing": true}\n'
            target_path.write_text(target_text, encoding="utf-8")

            self.assertFalse(migrate_config_file(config_dir))
            self.assertEqual(target_path.read_text(encoding="utf-8"), target_text)
            self.assertTrue(legacy_path.exists())

    def test_concurrent_target_creation_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory(dir="temp") as directory:
            config_dir = Path(directory)
            legacy_path = config_dir / LEGACY_CONFIG_FILENAME
            target_path = config_dir / TARGET_CONFIG_FILENAME
            legacy_path.write_text('{"bot_language": "zh"}', encoding="utf-8")

            def create_competing_target(_source, destination):
                Path(destination).write_text('{"concurrent": true}\n', encoding="utf-8")
                raise FileExistsError(destination)

            with patch.object(config_migration.os, "link", create_competing_target):
                self.assertFalse(migrate_config_file(config_dir))

            self.assertEqual(
                target_path.read_text(encoding="utf-8"),
                '{"concurrent": true}\n',
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
            self.assertIn('\n    "living_memory": {', result)
            self.assertIn('\n        "enabled": true,', result)
            self.assertIn('"提示": "星光"', result)
            self.assertNotIn("\\u", result)


if __name__ == "__main__":
    unittest.main()
