from __future__ import annotations

import ctypes
import errno
import hashlib
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

MODULE_PATH = Path(__file__).parents[1] / "memory" / "data_migration.py"
MODULE_SPEC = importlib.util.spec_from_file_location("memory_data_migration", MODULE_PATH)
if MODULE_SPEC is None or MODULE_SPEC.loader is None:
    raise RuntimeError(f"无法加载数据迁移模块: {MODULE_PATH}")
data_migration = importlib.util.module_from_spec(MODULE_SPEC)
sys.modules[MODULE_SPEC.name] = data_migration
MODULE_SPEC.loader.exec_module(data_migration)


def _source_file_snapshot(directory: Path) -> dict[str, tuple[int, str, int]]:
    snapshot = {}
    for path in sorted(directory.rglob("*")):
        if path.is_file():
            snapshot[path.relative_to(directory).as_posix()] = (
                path.stat().st_size,
                hashlib.sha256(path.read_bytes()).hexdigest(),
                path.stat().st_mtime_ns,
            )
    return snapshot


def _directory_structure_snapshot(directory: Path) -> dict[str, tuple[str, bytes | None]]:
    snapshot = {}
    for path in sorted(directory.rglob("*")):
        relative = path.relative_to(directory).as_posix()
        if path.is_symlink():
            snapshot[relative] = ("symlink", os.fsencode(os.readlink(path)))
        elif path.is_dir():
            snapshot[relative] = ("directory", None)
        elif path.is_file():
            snapshot[relative] = ("file", path.read_bytes())
    return snapshot


def _create_crashed_wal_db(path: Path, row_count: int, page_size: int | None = None) -> None:
    script = """
import os
import sqlite3
import sys

path = sys.argv[1]
row_count = int(sys.argv[2])
page_size = int(sys.argv[3]) if len(sys.argv) > 3 else None
db = sqlite3.connect(path)
if page_size is not None:
    db.execute(f"PRAGMA page_size={page_size}")
db.execute("PRAGMA journal_mode=WAL")
db.execute("PRAGMA wal_autocheckpoint=0")
db.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY, content TEXT)")
db.execute("CREATE TABLE conversations (id INTEGER PRIMARY KEY)")
db.commit()
db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
db.executemany(
    "INSERT INTO documents(content) VALUES (?)",
    [(f"row-{index}",) for index in range(row_count)],
)
db.executemany(
    "INSERT INTO conversations DEFAULT VALUES",
    [() for _ in range(row_count + 1)],
)
db.commit()
os._exit(0)
"""
    arguments = [sys.executable, "-c", script, str(path), str(row_count)]
    if page_size is not None:
        arguments.append(str(page_size))
    completed = subprocess.run(arguments, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"创建异常退出 WAL 数据库失败: {path}")


def _create_crashed_runtime_wal(path: Path, row_count: int = 1) -> None:
    script = """
import os
import sqlite3
import sys

path = sys.argv[1]
row_count = int(sys.argv[2])
db = sqlite3.connect(path)
db.execute("PRAGMA journal_mode=WAL")
db.execute("PRAGMA wal_autocheckpoint=0")
db.executemany(
    "INSERT INTO documents DEFAULT VALUES",
    [() for _ in range(row_count)],
)
db.commit()
os._exit(0)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, str(path), str(row_count)],
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"创建运行期异常退出 WAL 数据库失败: {path}")


def _wal_header(page_size: int) -> bytes:
    header = bytearray()
    header.extend(b"7\x7f\x06\x82")
    header.extend((3_007_000).to_bytes(4, "big"))
    header.extend(page_size.to_bytes(4, "big"))
    header.extend((0).to_bytes(4, "big"))
    header.extend((0x12345678).to_bytes(4, "big"))
    header.extend((0x9ABCDEF0).to_bytes(4, "big"))
    words = [int.from_bytes(header[offset : offset + 4], "little") for offset in range(0, 24, 4)]
    checksum_a = 0
    checksum_b = 0
    for offset in range(0, len(words), 2):
        checksum_a = (checksum_a + words[offset] + checksum_b) & 0xFFFFFFFF
        checksum_b = (checksum_b + words[offset + 1] + checksum_a) & 0xFFFFFFFF
    header.extend(checksum_a.to_bytes(4, "big"))
    header.extend(checksum_b.to_bytes(4, "big"))
    return bytes(header)


class DataMigrationTests(unittest.TestCase):
    def test_overlapping_source_and_root_are_rejected_before_any_write(self) -> None:
        def assert_rejected_without_changes(base: Path, source: Path, target_root: Path) -> None:
            base_snapshot = _directory_structure_snapshot(base)

            with self.assertRaisesRegex(data_migration.DataMigrationError, "重叠"):
                data_migration.ensure_memory_data(source, target_root)

            self.assertEqual(_directory_structure_snapshot(base), base_snapshot)
            self.assertFalse((target_root / data_migration.STAGING_NAME).exists())
            self.assertFalse((target_root / data_migration.STATE_FILENAME).exists())
            self.assertFalse((target_root / "memory").exists())

        for relationship in ("same", "source-contains-root", "root-contains-source"):
            with (
                self.subTest(relationship=relationship),
                tempfile.TemporaryDirectory(dir="temp") as directory,
            ):
                base = Path(directory)
                if relationship == "same":
                    source = base / "shared"
                    target_root = source
                    source.mkdir()
                elif relationship == "source-contains-root":
                    source = base / "legacy"
                    target_root = source / "nested-root"
                    target_root.mkdir(parents=True)
                else:
                    target_root = base / "current"
                    source = target_root / "nested-legacy"
                    source.mkdir(parents=True)
                (source / "payload.bin").write_bytes(b"unchanged-source")
                (target_root / "root-marker.bin").write_bytes(b"unchanged-root")

                assert_rejected_without_changes(base, source, target_root)

        for relationship in (
            "root-alias-inside-source",
            "source-alias-inside-root",
            "different-aliases-same-directory",
            "chained-symlink-ancestors",
        ):
            with (
                self.subTest(real_relationship=relationship),
                tempfile.TemporaryDirectory(dir="temp") as directory,
            ):
                base = Path(directory)
                if relationship == "root-alias-inside-source":
                    source = base / "legacy"
                    source.mkdir()
                    (source / "payload.bin").write_bytes(b"unchanged-source")
                    root_alias = base / "root-alias"
                    root_alias.symlink_to(source, target_is_directory=True)
                    target_root = root_alias / "nested-root"
                    target_root.mkdir()
                elif relationship == "source-alias-inside-root":
                    target_root = base / "current"
                    source_real = target_root / "nested-legacy"
                    source_real.mkdir(parents=True)
                    source_alias = base / "source-alias"
                    source_alias.symlink_to(target_root, target_is_directory=True)
                    source = source_alias / source_real.name
                    (source / "payload.bin").write_bytes(b"unchanged-source")
                elif relationship == "different-aliases-same-directory":
                    shared = base / "shared"
                    shared.mkdir()
                    source = base / "source-alias"
                    target_root = base / "root-alias"
                    source.symlink_to(shared, target_is_directory=True)
                    target_root.symlink_to(shared, target_is_directory=True)
                    (source / "payload.bin").write_bytes(b"unchanged-source")
                else:
                    real_parent = base / "real-parent"
                    source_real = real_parent / "legacy"
                    target_root_real = source_real / "nested-root"
                    target_root_real.mkdir(parents=True)
                    source_hop = base / "source-hop"
                    source_hop.symlink_to(real_parent, target_is_directory=True)
                    source_entry = base / "source-entry"
                    source_entry.symlink_to(source_hop, target_is_directory=True)
                    root_hop = base / "root-hop"
                    root_hop.symlink_to(real_parent, target_is_directory=True)
                    root_entry = base / "root-entry"
                    root_entry.symlink_to(root_hop, target_is_directory=True)
                    source = source_entry / "legacy"
                    target_root = root_entry / "legacy" / "nested-root"
                    (source / "payload.bin").write_bytes(b"unchanged-source")

                (target_root / "root-marker.bin").write_bytes(b"unchanged-root")
                assert_rejected_without_changes(base, source, target_root)

    def _staging_identity(self, source: Path, target_root: Path) -> dict:
        return {
            "kind": data_migration.MIGRATION_KIND,
            "version": data_migration.MIGRATION_STATE_VERSION,
            "source": str(source.resolve()),
            "target": str((target_root / "memory").resolve()),
            "staging": data_migration.STAGING_NAME,
        }

    def _leave_ready_staging(self, source: Path, target_root: Path) -> tuple[Path, dict]:
        with patch.object(
            data_migration,
            "_atomic_publish_noreplace",
            side_effect=SystemExit("模拟 READY 写入后进程崩溃"),
        ):
            with self.assertRaises(SystemExit):
                data_migration.ensure_memory_data(source, target_root)
        staging = target_root / data_migration.STAGING_NAME
        state = json.loads(
            (staging / data_migration.STATE_FILENAME).read_text(encoding="utf-8")
        )
        self.assertEqual(state["status"], "READY")
        return staging, state

    def test_ready_staging_is_reverified_and_published_without_changing_source(self) -> None:
        with tempfile.TemporaryDirectory(dir="temp") as directory:
            root = Path(directory)
            source = root / "legacy"
            target_root = root / "current"
            source.mkdir()
            (source / "payload.txt").write_text("stable", encoding="utf-8")
            database = source / "livingmemory.db"
            with closing(sqlite3.connect(database)) as db, db:
                db.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY)")
                db.executemany(
                    "INSERT INTO documents DEFAULT VALUES",
                    [(), (), ()],
                )
            source_snapshot = _source_file_snapshot(source)
            staging, ready = self._leave_ready_staging(source, target_root)
            self.assertEqual(ready["kind"], data_migration.MIGRATION_KIND)
            self.assertEqual(ready["version"], data_migration.MIGRATION_STATE_VERSION)
            self.assertEqual(ready["target"], str((target_root / "memory").resolve()))
            self.assertEqual(ready["staging"], data_migration.STAGING_NAME)

            result = data_migration.ensure_memory_data(source, target_root)

            self.assertEqual(_source_file_snapshot(source), source_snapshot)
            self.assertTrue(result.migrated)
            self.assertEqual(result.state, ready)
            self.assertFalse(staging.exists())
            self.assertTrue((result.target_dir / "payload.txt").is_file())
            with closing(sqlite3.connect(result.target_dir / "livingmemory.db")) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 3)

            existing = data_migration.ensure_memory_data(source, target_root)
            self.assertFalse(existing.migrated)
            self.assertEqual(existing.state, ready)

            state_path = result.target_dir / data_migration.STATE_FILENAME
            legacy = {
                "source": ready["source"],
                "source_present": ready["source_present"],
                "status": "READY",
                "verification": ready["verification"],
            }
            state_path.write_text(json.dumps(legacy), encoding="utf-8")

            upgraded = data_migration.ensure_memory_data(source, target_root)

            self.assertFalse(upgraded.migrated)
            self.assertEqual(upgraded.state["verification"], legacy["verification"])
            self.assertEqual(upgraded.state["source_present"], legacy["source_present"])
            for key, expected in self._staging_identity(source, target_root).items():
                self.assertEqual(upgraded.state[key], expected)
            self.assertEqual(
                json.loads(state_path.read_text(encoding="utf-8")),
                upgraded.state,
            )

    def test_tampered_or_corrupt_ready_staging_is_never_published(self) -> None:
        for damage_kind in ("plain", "sqlite"):
            with self.subTest(damage_kind=damage_kind), tempfile.TemporaryDirectory(dir="temp") as directory:
                root = Path(directory)
                source = root / "legacy"
                target_root = root / "current"
                source.mkdir()
                (source / "payload.txt").write_text("stable", encoding="utf-8")
                database = source / "livingmemory.db"
                with closing(sqlite3.connect(database)) as db, db:
                    db.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY)")
                    db.execute("INSERT INTO documents DEFAULT VALUES")
                source_snapshot = _source_file_snapshot(source)
                staging, ready = self._leave_ready_staging(source, target_root)
                if damage_kind == "plain":
                    (staging / "payload.txt").write_text("tampered", encoding="utf-8")
                else:
                    (staging / "livingmemory.db").write_bytes(b"not a sqlite database")
                state_path = staging / data_migration.STATE_FILENAME
                ready_state_bytes = state_path.read_bytes()

                with self.assertRaises(data_migration.DataMigrationError):
                    data_migration.ensure_memory_data(source, target_root)

                self.assertEqual(_source_file_snapshot(source), source_snapshot)
                self.assertFalse((target_root / "memory").exists())
                self.assertTrue(staging.is_dir())
                self.assertEqual(state_path.read_bytes(), ready_state_bytes)
                preserved = json.loads(state_path.read_text(encoding="utf-8"))
                self.assertEqual(preserved, ready)
                self.assertEqual(preserved["status"], "READY")

        for state_format in ("new", "legacy"):
            with (
                self.subTest(state_format=state_format, target_damage="sqlite"),
                tempfile.TemporaryDirectory(dir="temp") as directory,
            ):
                root = Path(directory)
                source = root / "legacy"
                target_root = root / "current"
                source.mkdir()
                (source / "payload.txt").write_text("stable", encoding="utf-8")
                database = source / "livingmemory.db"
                with closing(sqlite3.connect(database)) as db, db:
                    db.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY)")
                    db.execute("INSERT INTO documents DEFAULT VALUES")
                result = data_migration.ensure_memory_data(source, target_root)
                state_path = result.target_dir / data_migration.STATE_FILENAME
                if state_format == "legacy":
                    legacy = {
                        "source": result.state["source"],
                        "source_present": result.state["source_present"],
                        "status": "READY",
                        "verification": result.state["verification"],
                    }
                    state_path.write_text(json.dumps(legacy), encoding="utf-8")
                state_bytes = state_path.read_bytes()
                (result.target_dir / "livingmemory.db").write_bytes(b"not a sqlite database")

                with self.assertRaises(data_migration.DataMigrationError):
                    data_migration.ensure_memory_data(source, target_root)

                self.assertEqual(state_path.read_bytes(), state_bytes)

    def test_published_ready_target_accepts_runtime_mutations_without_refreshing_audit(self) -> None:
        with tempfile.TemporaryDirectory(dir="temp") as directory:
            root = Path(directory)
            source = root / "legacy"
            target_root = root / "current"
            backup_dir = source / "backups"
            backup_dir.mkdir(parents=True)
            (source / "settings.json").write_text('{"version": 1}\n', encoding="utf-8")
            for db_path in (source / "livingmemory.db", backup_dir / "old.db"):
                with closing(sqlite3.connect(db_path)) as db, db:
                    db.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY, content TEXT)")
                    db.executemany(
                        "INSERT INTO documents(content) VALUES (?)",
                        [("one",), ("two",)],
                    )

            result = data_migration.ensure_memory_data(source, target_root)
            target = result.target_dir
            state_path = target / data_migration.STATE_FILENAME
            original_state = json.loads(state_path.read_text(encoding="utf-8"))
            original_verification = original_state["verification"]
            old_main_hash = hashlib.sha256((target / "livingmemory.db").read_bytes()).hexdigest()

            with closing(sqlite3.connect(target / "livingmemory.db")) as db:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("DELETE FROM documents WHERE id = 1")
                db.executemany(
                    "INSERT INTO documents(content) VALUES (?)",
                    [("runtime-a",), ("runtime-b",)],
                )
                db.commit()
            with closing(sqlite3.connect(target / "livingmemory.db")) as db:
                db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self.assertNotEqual(
                hashlib.sha256((target / "livingmemory.db").read_bytes()).hexdigest(),
                old_main_hash,
            )
            (target / "backups" / "old.db").unlink()
            with closing(sqlite3.connect(target / "backups" / "new.db")) as db, db:
                db.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY)")
                db.execute("INSERT INTO messages DEFAULT VALUES")
            (target / "settings.json").write_text('{"version": 2}\n', encoding="utf-8")
            (target / "runtime.json").write_text('{"active": true}\n', encoding="utf-8")

            reused = data_migration.ensure_memory_data(source, target_root)

            self.assertFalse(reused.migrated)
            self.assertEqual(reused.state, original_state)
            self.assertEqual(reused.state["verification"], original_verification)
            self.assertEqual(json.loads(state_path.read_text(encoding="utf-8")), original_state)

    def test_published_ready_target_accepts_wal_sidecars_disappearing_after_close(self) -> None:
        with tempfile.TemporaryDirectory(dir="temp") as directory:
            root = Path(directory)
            source = root / "legacy"
            target_root = root / "current"
            source.mkdir()
            database_names = ("livingmemory.db", "conversations.db")
            for name in database_names:
                with closing(sqlite3.connect(source / name)) as db, db:
                    db.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY)")
            for index in range(5):
                (source / f"runtime-{index}.json").write_text("{}", encoding="utf-8")
            result = data_migration.ensure_memory_data(source, target_root)
            target = result.target_dir
            connections = []
            try:
                for name in database_names:
                    db = sqlite3.connect(target / name)
                    connections.append(db)
                    db.execute("PRAGMA journal_mode=WAL")
                    db.execute("PRAGMA wal_autocheckpoint=0")
                    db.execute("INSERT INTO documents DEFAULT VALUES")
                    db.commit()
                sidecars = [path for path in target.iterdir() if path.name.endswith(("-wal", "-shm"))]
                self.assertEqual(len(sidecars), 4)
                self.assertEqual(len(list(target.iterdir())), 12)
            finally:
                for db in connections:
                    db.close()
            self.assertEqual(
                len([path for path in target.iterdir() if path.name.endswith(("-wal", "-shm"))]),
                0,
            )
            self.assertEqual(len(list(target.iterdir())), 8)

            reused = data_migration.ensure_memory_data(source, target_root)

            self.assertFalse(reused.migrated)
            self.assertEqual(reused.state, result.state)

    def test_published_ready_validation_reads_active_wal_without_checkpoint_or_writes(self) -> None:
        with tempfile.TemporaryDirectory(dir="temp") as directory:
            root = Path(directory)
            source = root / "legacy"
            target_root = root / "current"
            source.mkdir()
            with closing(sqlite3.connect(source / "livingmemory.db")) as db, db:
                db.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY)")
            result = data_migration.ensure_memory_data(source, target_root)
            database = result.target_dir / "livingmemory.db"
            connection = sqlite3.connect(database)
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA wal_autocheckpoint=0")
                connection.execute("INSERT INTO documents DEFAULT VALUES")
                connection.commit()
                wal = Path(f"{database}-wal")
                shm = Path(f"{database}-shm")
                self.assertTrue(wal.is_file())
                self.assertGreater(wal.stat().st_size, 0)
                before = {
                    path.name: (path.read_bytes(), path.stat().st_mtime_ns)
                    for path in (database, wal)
                }

                reused = data_migration.ensure_memory_data(source, target_root)

                self.assertFalse(reused.migrated)
                self.assertEqual(
                    {
                        path.name: (path.read_bytes(), path.stat().st_mtime_ns)
                        for path in (database, wal)
                    },
                    before,
                )
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0],
                    1,
                )
            finally:
                connection.close()

    def test_published_ready_validation_uses_private_snapshot_and_preserves_target(self) -> None:
        for shm_kind in ("missing", "zeroed", "stale"):
            with self.subTest(shm_kind=shm_kind), tempfile.TemporaryDirectory(dir="temp") as directory:
                root = Path(directory)
                source = root / "legacy"
                target_root = root / "current"
                source.mkdir()
                with closing(sqlite3.connect(source / "livingmemory.db")) as db, db:
                    db.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY)")
                result = data_migration.ensure_memory_data(source, target_root)
                database = result.target_dir / "livingmemory.db"
                _create_crashed_runtime_wal(database, 2)
                wal = Path(f"{database}-wal")
                shm = Path(f"{database}-shm")
                self.assertGreater(wal.stat().st_size, 0)
                if shm_kind == "missing":
                    shm.unlink()
                elif shm_kind == "zeroed":
                    shm.write_bytes(b"\x00" * shm.stat().st_size)
                else:
                    shm.write_bytes(b"stale-shm")
                before = _source_file_snapshot(result.target_dir)
                real_connect = sqlite3.connect
                validation_paths = []

                def tracked_connect(database_name, *args, **kwargs):
                    validation_paths.append(str(database_name))
                    return real_connect(database_name, *args, **kwargs)

                with patch.object(data_migration.sqlite3, "connect", side_effect=tracked_connect):
                    reused = data_migration.ensure_memory_data(source, target_root)

                self.assertFalse(reused.migrated)
                self.assertEqual(_source_file_snapshot(result.target_dir), before)
                self.assertTrue(validation_paths)
                self.assertTrue(
                    all(".memory.verify-" in path for path in validation_paths),
                    validation_paths,
                )
                self.assertTrue(
                    all(str(result.target_dir.resolve()) not in path for path in validation_paths),
                    validation_paths,
                )
                self.assertEqual(
                    list(target_root.glob(".memory.verify-*")),
                    [],
                )

    def test_published_ready_validation_failure_preserves_target_and_cleans_snapshot(self) -> None:
        for damage_kind in ("wal", "database"):
            with self.subTest(damage_kind=damage_kind), tempfile.TemporaryDirectory(dir="temp") as directory:
                root = Path(directory)
                source = root / "legacy"
                target_root = root / "current"
                source.mkdir()
                with closing(sqlite3.connect(source / "livingmemory.db")) as db, db:
                    db.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY)")
                result = data_migration.ensure_memory_data(source, target_root)
                database = result.target_dir / "livingmemory.db"
                if damage_kind == "wal":
                    _create_crashed_runtime_wal(database)
                    wal = Path(f"{database}-wal")
                    damaged = bytearray(wal.read_bytes())
                    damaged[16] ^= 0xFF
                    wal.write_bytes(damaged)
                    Path(f"{database}-shm").unlink(missing_ok=True)
                else:
                    database.write_bytes(b"not a sqlite database")
                before = _source_file_snapshot(result.target_dir)

                with self.assertRaises(data_migration.DataMigrationError):
                    data_migration.ensure_memory_data(source, target_root)

                self.assertEqual(_source_file_snapshot(result.target_dir), before)
                self.assertEqual(list(target_root.glob(".memory.verify-*")), [])

    def test_published_ready_validation_rejects_concurrent_target_changes(self) -> None:
        with tempfile.TemporaryDirectory(dir="temp") as directory:
            root = Path(directory)
            source = root / "legacy"
            target_root = root / "current"
            source.mkdir()
            with closing(sqlite3.connect(source / "livingmemory.db")) as db, db:
                db.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY)")
            result = data_migration.ensure_memory_data(source, target_root)
            runtime_marker = result.target_dir / "runtime.txt"
            runtime_marker.write_text("0", encoding="utf-8")
            real_copy = data_migration._copy_plain_tree
            copy_count = 0

            def changing_copy(source_dir, target_dir):
                nonlocal copy_count
                real_copy(source_dir, target_dir)
                if source_dir == result.target_dir:
                    copy_count += 1
                    runtime_marker.write_text(str(copy_count), encoding="utf-8")

            with patch.object(data_migration, "_copy_plain_tree", side_effect=changing_copy):
                with self.assertRaisesRegex(data_migration.DataMigrationError, "稳定"):
                    data_migration.ensure_memory_data(source, target_root)

            self.assertGreaterEqual(copy_count, 1)
            self.assertEqual(list(target_root.glob(".memory.verify-*")), [])
            self.assertFalse(Path(f"{result.target_dir / 'livingmemory.db'}-shm").exists())

    def test_legacy_ready_upgrade_only_changes_state_after_snapshot_validation(self) -> None:
        with tempfile.TemporaryDirectory(dir="temp") as directory:
            root = Path(directory)
            source = root / "legacy"
            target_root = root / "current"
            source.mkdir()
            with closing(sqlite3.connect(source / "livingmemory.db")) as db, db:
                db.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY)")
            result = data_migration.ensure_memory_data(source, target_root)
            state_path = result.target_dir / data_migration.STATE_FILENAME
            legacy = {
                "source": result.state["source"],
                "source_present": result.state["source_present"],
                "status": "READY",
                "verification": result.state["verification"],
            }
            state_path.write_text(json.dumps(legacy), encoding="utf-8")
            database = result.target_dir / "livingmemory.db"
            _create_crashed_runtime_wal(database)
            Path(f"{database}-shm").unlink(missing_ok=True)
            before = _source_file_snapshot(result.target_dir)

            upgraded = data_migration.ensure_memory_data(source, target_root)

            after = _source_file_snapshot(result.target_dir)
            self.assertEqual(
                {path: entry for path, entry in after.items() if path != data_migration.STATE_FILENAME},
                {path: entry for path, entry in before.items() if path != data_migration.STATE_FILENAME},
            )
            self.assertNotEqual(after[data_migration.STATE_FILENAME], before[data_migration.STATE_FILENAME])
            self.assertEqual(upgraded.state["verification"], legacy["verification"])
            self.assertNotIn(".memory.verify-", json.dumps(upgraded.state))
            self.assertEqual(list(target_root.glob(".memory.verify-*")), [])

    def test_legacy_ready_upgrade_preserves_verification_after_runtime_changes(self) -> None:
        with tempfile.TemporaryDirectory(dir="temp") as directory:
            root = Path(directory)
            source = root / "legacy"
            target_root = root / "current"
            source.mkdir()
            database = source / "livingmemory.db"
            with closing(sqlite3.connect(database)) as db, db:
                db.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY)")
                db.execute("INSERT INTO documents DEFAULT VALUES")
            result = data_migration.ensure_memory_data(source, target_root)
            state_path = result.target_dir / data_migration.STATE_FILENAME
            verification = result.state["verification"]
            legacy = {
                "source": result.state["source"],
                "source_present": result.state["source_present"],
                "status": "READY",
                "verification": verification,
            }
            state_path.write_text(json.dumps(legacy, separators=(",", ":")), encoding="utf-8")
            with closing(sqlite3.connect(result.target_dir / "livingmemory.db")) as db, db:
                db.execute("INSERT INTO documents DEFAULT VALUES")
            (result.target_dir / "runtime.json").write_text("changed", encoding="utf-8")

            upgraded = data_migration.ensure_memory_data(source, target_root)

            self.assertFalse(upgraded.migrated)
            self.assertEqual(upgraded.state["verification"], verification)
            self.assertEqual(
                json.loads(state_path.read_text(encoding="utf-8"))["verification"],
                verification,
            )
            for key, expected in self._staging_identity(source, target_root).items():
                self.assertEqual(upgraded.state[key], expected)

    def test_corrupt_current_runtime_sqlite_is_rejected_without_changing_state(self) -> None:
        with tempfile.TemporaryDirectory(dir="temp") as directory:
            root = Path(directory)
            source = root / "legacy"
            target_root = root / "current"
            source.mkdir()
            with closing(sqlite3.connect(source / "livingmemory.db")) as db, db:
                db.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY)")
            result = data_migration.ensure_memory_data(source, target_root)
            state_path = result.target_dir / data_migration.STATE_FILENAME
            state_bytes = state_path.read_bytes()
            corrupt = result.target_dir / "backups" / "new-runtime.db"
            corrupt.parent.mkdir()
            corrupt.write_bytes(b"not a sqlite database")

            with self.assertRaisesRegex(data_migration.DataMigrationError, "SQLite"):
                data_migration.ensure_memory_data(source, target_root)

            self.assertEqual(state_path.read_bytes(), state_bytes)

    def test_missing_recorded_core_sqlite_is_rejected_without_changing_state(self) -> None:
        with tempfile.TemporaryDirectory(dir="temp") as directory:
            root = Path(directory)
            source = root / "legacy"
            target_root = root / "current"
            source.mkdir()
            with closing(sqlite3.connect(source / "livingmemory.db")) as db, db:
                db.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY)")
            result = data_migration.ensure_memory_data(source, target_root)
            state_path = result.target_dir / data_migration.STATE_FILENAME
            state_bytes = state_path.read_bytes()
            (result.target_dir / "livingmemory.db").unlink()

            with self.assertRaisesRegex(data_migration.DataMigrationError, "缺少预期核心 SQLite"):
                data_migration.ensure_memory_data(source, target_root)

            self.assertEqual(state_path.read_bytes(), state_bytes)

    def test_ready_staging_runtime_style_changes_remain_strict_and_are_not_published(self) -> None:
        for mutation in ("row", "backup", "plain"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory(dir="temp") as directory:
                root = Path(directory)
                source = root / "legacy"
                target_root = root / "current"
                backup_dir = source / "backups"
                backup_dir.mkdir(parents=True)
                (source / "settings.json").write_text("original", encoding="utf-8")
                for db_path in (source / "livingmemory.db", backup_dir / "old.db"):
                    with closing(sqlite3.connect(db_path)) as db, db:
                        db.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY)")
                        db.execute("INSERT INTO documents DEFAULT VALUES")
                staging, ready = self._leave_ready_staging(source, target_root)
                state_path = staging / data_migration.STATE_FILENAME
                state_bytes = state_path.read_bytes()
                if mutation == "row":
                    with closing(sqlite3.connect(staging / "livingmemory.db")) as db, db:
                        db.execute("INSERT INTO documents DEFAULT VALUES")
                elif mutation == "backup":
                    (staging / "backups" / "old.db").unlink()
                else:
                    (staging / "settings.json").write_text("runtime", encoding="utf-8")

                with self.assertRaises(data_migration.DataMigrationError):
                    data_migration.ensure_memory_data(source, target_root)

                self.assertFalse((target_root / "memory").exists())
                self.assertTrue(staging.is_dir())
                self.assertEqual(state_path.read_bytes(), state_bytes)
                self.assertEqual(json.loads(state_path.read_text(encoding="utf-8")), ready)

    def test_ready_and_failed_staging_identity_must_be_complete_and_match(self) -> None:
        mismatches = {
            "kind": "other-migration",
            "version": data_migration.MIGRATION_STATE_VERSION + 1,
            "source": "/other/source",
            "target": "/other/target",
            "staging": ".other.staging",
        }
        for status in ("READY", "FAILED"):
            for field in mismatches:
                for mutation in ("missing", "mismatch"):
                    with (
                        self.subTest(status=status, field=field, mutation=mutation),
                        tempfile.TemporaryDirectory(dir="temp") as directory,
                    ):
                        root = Path(directory)
                        source = root / "legacy"
                        target_root = root / "current"
                        source.mkdir()
                        (source / "payload.txt").write_text("source", encoding="utf-8")
                        if status == "READY":
                            staging, state = self._leave_ready_staging(source, target_root)
                        else:
                            staging = target_root / data_migration.STAGING_NAME
                            staging.mkdir(parents=True)
                            (staging / "old-partial-copy").write_text("preserve", encoding="utf-8")
                            state = {
                                "status": "FAILED",
                                **self._staging_identity(source, target_root),
                                "error": "previous failure",
                            }
                        if mutation == "missing":
                            state.pop(field)
                        else:
                            state[field] = mismatches[field]
                        (staging / data_migration.STATE_FILENAME).write_text(
                            json.dumps(state),
                            encoding="utf-8",
                        )
                        staging_snapshot = _source_file_snapshot(staging)

                        expected_error = (
                            "schema" if status == "READY" and mutation == "missing" else "不匹配"
                        )
                        with self.assertRaisesRegex(
                            data_migration.DataMigrationError,
                            expected_error,
                        ):
                            data_migration.ensure_memory_data(source, target_root)

                        self.assertTrue(staging.is_dir())
                        self.assertEqual(_source_file_snapshot(staging), staging_snapshot)
                        self.assertFalse((target_root / "memory").exists())

        for field in mismatches:
            for mutation in ("missing", "mismatch"):
                with (
                    self.subTest(target_field=field, mutation=mutation),
                    tempfile.TemporaryDirectory(dir="temp") as directory,
                ):
                    root = Path(directory)
                    source = root / "legacy"
                    target_root = root / "current"
                    source.mkdir()
                    (source / "payload.txt").write_text("source", encoding="utf-8")
                    result = data_migration.ensure_memory_data(source, target_root)
                    state_path = result.target_dir / data_migration.STATE_FILENAME
                    state = result.state.copy()
                    if mutation == "missing":
                        state.pop(field)
                    else:
                        state[field] = mismatches[field]
                    state_path.write_text(json.dumps(state), encoding="utf-8")
                    state_bytes = state_path.read_bytes()

                    with self.assertRaises(data_migration.DataMigrationError):
                        data_migration.ensure_memory_data(source, target_root)

                    self.assertEqual(state_path.read_bytes(), state_bytes)

        for location in ("staging", "target-new", "target-legacy"):
            for mutation in ("missing-source-present", "missing-verification", "extra-field"):
                with (
                    self.subTest(ready_schema_location=location, mutation=mutation),
                    tempfile.TemporaryDirectory(dir="temp") as directory,
                ):
                    root = Path(directory)
                    source = root / "legacy"
                    target_root = root / "current"
                    source.mkdir()
                    (source / "payload.txt").write_text("source", encoding="utf-8")
                    if location == "staging":
                        state_dir, state = self._leave_ready_staging(source, target_root)
                        validation_name = "_validate_ready_contents"
                    else:
                        result = data_migration.ensure_memory_data(source, target_root)
                        state_dir = result.target_dir
                        state = result.state.copy()
                        validation_name = "_validate_published_ready_contents"
                        if location == "target-legacy":
                            state = {
                                "source": state["source"],
                                "source_present": state["source_present"],
                                "status": state["status"],
                                "verification": state["verification"],
                            }
                    if mutation == "missing-source-present":
                        state.pop("source_present")
                    elif mutation == "missing-verification":
                        state.pop("verification")
                    else:
                        state["unexpected"] = "must fail closed"
                    state_path = state_dir / data_migration.STATE_FILENAME
                    state_path.write_text(json.dumps(state), encoding="utf-8")
                    state_bytes = state_path.read_bytes()

                    with patch.object(
                        data_migration,
                        validation_name,
                        side_effect=AssertionError("不得验证内容"),
                    ):
                        with self.assertRaisesRegex(data_migration.DataMigrationError, "schema"):
                            data_migration.ensure_memory_data(source, target_root)

                    self.assertEqual(state_path.read_bytes(), state_bytes)
                    if location == "staging":
                        self.assertTrue(state_dir.is_dir())
                        self.assertFalse((target_root / "memory").exists())

        for malformed in (
            "missing-verification",
            "extra-identity",
            "snapshot-dir",
            "staging-dir",
            "sibling-staging",
        ):
            with (
                self.subTest(malformed_legacy=malformed),
                tempfile.TemporaryDirectory(dir="temp") as directory,
            ):
                root = Path(directory)
                source = root / "legacy"
                target_root = root / "current"
                source.mkdir()
                (source / "payload.txt").write_text("source", encoding="utf-8")
                result = data_migration.ensure_memory_data(source, target_root)
                state_path = result.target_dir / data_migration.STATE_FILENAME
                legacy = {
                    "source": result.state["source"],
                    "source_present": result.state["source_present"],
                    "status": "READY",
                    "verification": result.state["verification"],
                }
                if malformed == "missing-verification":
                    legacy.pop("verification")
                elif malformed == "extra-identity":
                    legacy["kind"] = data_migration.MIGRATION_KIND
                elif malformed == "snapshot-dir":
                    (result.target_dir / data_migration.SOURCE_SNAPSHOT_NAME).mkdir()
                elif malformed == "staging-dir":
                    (result.target_dir / data_migration.STAGING_NAME).mkdir()
                else:
                    (target_root / data_migration.STAGING_NAME).mkdir()
                state_path.write_text(json.dumps(legacy), encoding="utf-8")
                state_bytes = state_path.read_bytes()

                with self.assertRaises(data_migration.DataMigrationError):
                    data_migration.ensure_memory_data(source, target_root)

                self.assertEqual(state_path.read_bytes(), state_bytes)

        with tempfile.TemporaryDirectory(dir="temp") as directory:
            root = Path(directory)
            source = root / "legacy"
            target_root = root / "current"
            source.mkdir()
            (source / "payload.txt").write_text("source", encoding="utf-8")
            result = data_migration.ensure_memory_data(source, target_root)
            state_path = result.target_dir / data_migration.STATE_FILENAME
            legacy = {
                "source": result.state["source"],
                "source_present": result.state["source_present"],
                "status": "READY",
                "verification": result.state["verification"],
            }
            state_path.write_text(json.dumps(legacy), encoding="utf-8")
            state_bytes = state_path.read_bytes()

            with patch.object(data_migration.os, "replace", side_effect=OSError("replace failed")):
                with self.assertRaises(data_migration.DataMigrationError):
                    data_migration.ensure_memory_data(source, target_root)

            self.assertEqual(state_path.read_bytes(), state_bytes)

    def test_ready_staging_with_different_source_is_preserved_and_not_published(self) -> None:
        with tempfile.TemporaryDirectory(dir="temp") as directory:
            root = Path(directory)
            original_source = root / "legacy-a"
            requested_source = root / "legacy-b"
            target_root = root / "current"
            original_source.mkdir()
            requested_source.mkdir()
            (original_source / "payload.txt").write_text("a", encoding="utf-8")
            (requested_source / "payload.txt").write_text("b", encoding="utf-8")
            staging, _ready = self._leave_ready_staging(original_source, target_root)
            staging_snapshot = _source_file_snapshot(staging)

            with self.assertRaisesRegex(data_migration.DataMigrationError, "来源不匹配"):
                data_migration.ensure_memory_data(requested_source, target_root)

            self.assertFalse((target_root / "memory").exists())
            self.assertTrue(staging.is_dir())
            self.assertEqual(_source_file_snapshot(staging), staging_snapshot)

        with tempfile.TemporaryDirectory(dir="temp") as directory:
            root = Path(directory)
            original_source = root / "legacy-a"
            requested_source = root / "legacy-b"
            target_root = root / "current"
            original_source.mkdir()
            requested_source.mkdir()
            (original_source / "payload.txt").write_text("a", encoding="utf-8")
            result = data_migration.ensure_memory_data(original_source, target_root)
            state_path = result.target_dir / data_migration.STATE_FILENAME
            legacy = {
                "source": result.state["source"],
                "source_present": result.state["source_present"],
                "status": "READY",
                "verification": result.state["verification"],
            }
            state_path.write_text(json.dumps(legacy), encoding="utf-8")
            state_bytes = state_path.read_bytes()

            with self.assertRaisesRegex(data_migration.DataMigrationError, "来源不匹配"):
                data_migration.ensure_memory_data(requested_source, target_root)

            self.assertEqual(state_path.read_bytes(), state_bytes)

    def test_failed_staging_rebuilds_only_when_source_matches(self) -> None:
        for source_matches in (True, False):
            with self.subTest(source_matches=source_matches), tempfile.TemporaryDirectory(dir="temp") as directory:
                root = Path(directory)
                source = root / "legacy"
                recorded_source = source if source_matches else root / "other-legacy"
                target_root = root / "current"
                staging = target_root / data_migration.STAGING_NAME
                source.mkdir()
                staging.mkdir(parents=True)
                (source / "payload.txt").write_text("rebuilt", encoding="utf-8")
                marker = staging / "old-partial-copy"
                marker.write_text("keep unless matched", encoding="utf-8")
                failed_state = {
                    "status": "FAILED",
                    **self._staging_identity(recorded_source, target_root),
                    "error": "previous failure",
                }
                (staging / data_migration.STATE_FILENAME).write_text(
                    json.dumps(failed_state),
                    encoding="utf-8",
                )

                if source_matches:
                    result = data_migration.ensure_memory_data(source, target_root)
                    self.assertTrue(result.migrated)
                    self.assertFalse(staging.exists())
                    self.assertEqual(
                        (result.target_dir / "payload.txt").read_text(encoding="utf-8"),
                        "rebuilt",
                    )
                    self.assertFalse((result.target_dir / marker.name).exists())
                else:
                    staging_snapshot = _source_file_snapshot(staging)
                    with self.assertRaisesRegex(data_migration.DataMigrationError, "来源不匹配"):
                        data_migration.ensure_memory_data(source, target_root)
                    self.assertFalse((target_root / "memory").exists())
                    self.assertTrue(staging.is_dir())
                    self.assertEqual(_source_file_snapshot(staging), staging_snapshot)

    def test_ready_recovery_publish_never_replaces_concurrent_target(self) -> None:
        with tempfile.TemporaryDirectory(dir="temp") as directory:
            root = Path(directory)
            source = root / "legacy"
            target_root = root / "current"
            source.mkdir()
            (source / "payload.txt").write_text("source", encoding="utf-8")
            staging, ready = self._leave_ready_staging(source, target_root)
            staging_snapshot = _directory_structure_snapshot(staging)
            target = target_root / "memory"
            real_publish = data_migration._atomic_publish_noreplace

            def create_target_then_publish(staging_dir, target_dir):
                target.mkdir()
                real_publish(staging_dir, target_dir)

            with patch.object(
                data_migration,
                "_atomic_publish_noreplace",
                side_effect=create_target_then_publish,
            ):
                with self.assertRaisesRegex(data_migration.DataMigrationError, "并发创建"):
                    data_migration.ensure_memory_data(source, target_root)

            self.assertTrue(target.is_dir())
            self.assertEqual(list(target.iterdir()), [])
            self.assertTrue(staging.is_dir())
            self.assertEqual(_directory_structure_snapshot(staging), staging_snapshot)
            state = json.loads(
                (staging / data_migration.STATE_FILENAME).read_text(encoding="utf-8")
            )
            self.assertEqual(state, ready)
            self.assertEqual(state["status"], "READY")
            self.assertFalse((staging / data_migration.SOURCE_SNAPSHOT_NAME).exists())

            target.rmdir()
            recovered = data_migration.ensure_memory_data(source, target_root)
            self.assertTrue(recovered.migrated)
            self.assertEqual(recovered.state, ready)
            self.assertFalse(staging.exists())
            self.assertEqual((target / "payload.txt").read_text(encoding="utf-8"), "source")

    def test_ready_publish_errors_preserve_staging_and_can_retry(self) -> None:
        class FailingRename:
            def __init__(self, error_number: int):
                self.error_number = error_number

            def __call__(self, *_args):
                ctypes.set_errno(self.error_number)
                return -1

        class FakeLibc:
            def __init__(self, error_number: int):
                self.renameat2 = FailingRename(error_number)

        for ready_origin in ("fresh", "recovery"):
            for failure_kind in ("enosys", "einval", "oserror"):
                with (
                    self.subTest(ready_origin=ready_origin, failure=failure_kind),
                    tempfile.TemporaryDirectory(dir="temp") as directory,
                ):
                    root = Path(directory)
                    source = root / "legacy"
                    target_root = root / "current"
                    source.mkdir()
                    (source / "payload.bin").write_bytes(b"verified-ready-payload")
                    if ready_origin == "recovery":
                        staging, ready = self._leave_ready_staging(source, target_root)
                        before = _directory_structure_snapshot(staging)

                    if failure_kind == "oserror":
                        failure_patch = patch.object(
                            data_migration,
                            "_atomic_publish_noreplace",
                            side_effect=OSError(errno.EIO, "simulated publish failure"),
                        )
                    else:
                        error_number = errno.ENOSYS if failure_kind == "enosys" else errno.EINVAL
                        failure_patch = patch.object(
                            data_migration.ctypes,
                            "CDLL",
                            return_value=FakeLibc(error_number),
                        )
                    with failure_patch:
                        with self.assertRaises(data_migration.DataMigrationError):
                            data_migration.ensure_memory_data(source, target_root)

                    staging = target_root / data_migration.STAGING_NAME
                    self.assertTrue(staging.is_dir())
                    preserved = json.loads(
                        (staging / data_migration.STATE_FILENAME).read_text(encoding="utf-8")
                    )
                    self.assertEqual(preserved["status"], "READY")
                    self.assertIn("verification", preserved)
                    for key in ("kind", "version", "source", "target", "staging"):
                        self.assertIn(key, preserved)
                    self.assertFalse((staging / data_migration.SOURCE_SNAPSHOT_NAME).exists())
                    if ready_origin == "recovery":
                        self.assertEqual(preserved, ready)
                        self.assertEqual(_directory_structure_snapshot(staging), before)

                    recovered = data_migration.ensure_memory_data(source, target_root)
                    self.assertTrue(recovered.migrated)
                    self.assertEqual(recovered.state, preserved)
                    self.assertFalse(staging.exists())
                    self.assertEqual(
                        (recovered.target_dir / "payload.bin").read_bytes(),
                        b"verified-ready-payload",
                    )

    def test_crashed_wal_databases_migrate_without_changing_source_manifest(self) -> None:
        with tempfile.TemporaryDirectory(dir="temp") as directory:
            root = Path(directory)
            source = root / "legacy"
            target_root = root / "current"
            source.mkdir()
            expected_counts = {}
            missing_shm_paths = []
            retained_shm_paths = []

            for index in range(6):
                parent = source / f"group-{index % 2}"
                parent.mkdir(exist_ok=True)
                db_path = parent / f"memory-{index}.db"
                row_count = index + 2
                _create_crashed_wal_db(db_path, row_count)
                wal_path = Path(f"{db_path}-wal")
                shm_path = Path(f"{db_path}-shm")
                self.assertTrue(wal_path.is_file())
                self.assertGreater(wal_path.stat().st_size, 0)
                self.assertTrue(shm_path.is_file())
                if index < 4:
                    shm_path.unlink()
                    missing_shm_paths.append(shm_path)
                else:
                    shm_path.write_bytes(b"\x00" * shm_path.stat().st_size)
                    retained_shm_paths.append(shm_path)
                expected_counts[db_path.relative_to(source).as_posix()] = {
                    "documents": row_count,
                    "conversations": row_count + 1,
                }

            (source / "settings.json").write_text('{"enabled": true}\n', encoding="utf-8")
            source_manifest = _source_file_snapshot(source)

            result = data_migration.ensure_memory_data(source, target_root)

            self.assertEqual(_source_file_snapshot(source), source_manifest)
            self.assertEqual(len(missing_shm_paths), 4)
            self.assertTrue(all(not path.exists() for path in missing_shm_paths))
            self.assertEqual(len(retained_shm_paths), 2)
            self.assertTrue(all(path.is_file() for path in retained_shm_paths))
            self.assertTrue(result.migrated)
            self.assertEqual(result.state["status"], "READY")
            self.assertEqual(
                sorted(result.state["verification"]["sqlite"]),
                sorted(expected_counts),
            )
            self.assertEqual(
                sorted(result.state["verification"]["hashes"]),
                sorted([*expected_counts, "settings.json"]),
            )
            for relative, counts in expected_counts.items():
                target_db = result.target_dir / relative
                self.assertTrue(target_db.is_file())
                self.assertFalse(Path(f"{target_db}-wal").exists())
                self.assertFalse(Path(f"{target_db}-shm").exists())
                with closing(sqlite3.connect(f"file:{target_db}?mode=ro", uri=True)) as migrated:
                    self.assertEqual(
                        migrated.execute("PRAGMA quick_check").fetchone()[0],
                        "ok",
                    )
                    self.assertEqual(
                        migrated.execute("PRAGMA integrity_check").fetchone()[0],
                        "ok",
                    )
                    self.assertEqual(
                        migrated.execute("SELECT COUNT(*) FROM documents").fetchone()[0],
                        counts["documents"],
                    )
                    self.assertEqual(
                        migrated.execute("SELECT COUNT(*) FROM conversations").fetchone()[0],
                        counts["conversations"],
                    )
                report = result.state["verification"]["sqlite"][relative]
                self.assertEqual(report["counts"], counts)
                self.assertEqual(report["snapshot_counts"], counts)

            self.assertFalse(
                any(path.name.startswith(".source") for path in result.target_dir.rglob("*"))
            )

    def test_wal_trailing_byte_migrates_committed_prefix_without_changing_source(self) -> None:
        with tempfile.TemporaryDirectory(dir="temp") as directory:
            root = Path(directory)
            source = root / "legacy"
            target_root = root / "current"
            source.mkdir()
            db_path = source / "livingmemory.db"
            _create_crashed_wal_db(db_path, 5)
            wal_path = Path(f"{db_path}-wal")
            with wal_path.open("ab") as wal:
                wal.write(b"x")
            source_manifest = _source_file_snapshot(source)

            result = data_migration.ensure_memory_data(source, target_root)

            self.assertEqual(_source_file_snapshot(source), source_manifest)
            with closing(sqlite3.connect(result.target_dir / "livingmemory.db")) as migrated:
                self.assertEqual(migrated.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 5)
                self.assertEqual(migrated.execute("SELECT COUNT(*) FROM conversations").fetchone()[0], 6)

    def test_empty_wal_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory(dir="temp") as directory:
            root = Path(directory)
            source = root / "legacy"
            target_root = root / "current"
            source.mkdir()
            db_path = source / "livingmemory.db"
            with closing(sqlite3.connect(db_path)) as db, db:
                db.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY)")
                db.execute("INSERT INTO documents DEFAULT VALUES")
            Path(f"{db_path}-wal").write_bytes(b"")
            source_manifest = _source_file_snapshot(source)

            result = data_migration.ensure_memory_data(source, target_root)

            self.assertEqual(_source_file_snapshot(source), source_manifest)
            self.assertEqual(
                result.state["verification"]["sqlite"]["livingmemory.db"]["counts"],
                {"documents": 1},
            )

    def test_wal_page_size_65536_is_not_treated_as_encoded_one(self) -> None:
        with tempfile.TemporaryDirectory(dir="temp") as directory:
            root = Path(directory)
            source = root / "legacy"
            target_root = root / "current"
            source.mkdir()
            db_path = source / "livingmemory.db"
            _create_crashed_wal_db(db_path, 2, page_size=65536)
            wal_path = Path(f"{db_path}-wal")
            header_page_size = int.from_bytes(wal_path.read_bytes()[8:12], "big")
            if header_page_size != 65536:
                wal_path.write_bytes(_wal_header(65536))
                data_migration._validate_wal_header(db_path)
                return

            result = data_migration.ensure_memory_data(source, target_root)

            with closing(sqlite3.connect(result.target_dir / "livingmemory.db")) as migrated:
                self.assertEqual(migrated.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 2)

    def test_short_nonempty_wal_fails_without_publishing(self) -> None:
        with tempfile.TemporaryDirectory(dir="temp") as directory:
            root = Path(directory)
            source = root / "legacy"
            target_root = root / "current"
            source.mkdir()
            db_path = source / "livingmemory.db"
            with closing(sqlite3.connect(db_path)) as db, db:
                db.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY)")
            Path(f"{db_path}-wal").write_bytes(b"short")
            source_manifest = _source_file_snapshot(source)

            with self.assertRaisesRegex(data_migration.DataMigrationError, "WAL 头损坏"):
                data_migration.ensure_memory_data(source, target_root)

            self.assertEqual(_source_file_snapshot(source), source_manifest)
            self.assertFalse((target_root / "memory").exists())

    def test_atomic_publish_fails_closed_when_renameat2_is_unsupported(self) -> None:
        class UnsupportedRename:
            def __call__(self, *_args):
                ctypes.set_errno(self.error_number)
                return -1

        class FakeLibc:
            renameat2 = UnsupportedRename()

        with tempfile.TemporaryDirectory(dir="temp") as directory:
            root = Path(directory)
            staging = root / "staging"
            target = root / "target"
            staging.mkdir()
            for error_number in (errno.ENOSYS, errno.EINVAL):
                with self.subTest(error_number=error_number):
                    FakeLibc.renameat2.error_number = error_number
                    with patch.object(data_migration.ctypes, "CDLL", return_value=FakeLibc()):
                        with self.assertRaisesRegex(data_migration.DataMigrationError, "不支持原子 no-replace"):
                            data_migration._atomic_publish_noreplace(staging, target)
                    self.assertTrue(staging.is_dir())
                    self.assertFalse(target.exists())

    def test_publish_never_replaces_concurrent_empty_directory(self) -> None:
        with tempfile.TemporaryDirectory(dir="temp") as directory:
            root = Path(directory)
            source = root / "legacy"
            target_root = root / "current"
            source.mkdir()
            (source / "payload.txt").write_text("source", encoding="utf-8")
            target = target_root / "memory"
            real_atomic_json = data_migration._atomic_json
            written_states = []

            def create_target_before_ready_publish(path, value):
                written_states.append(dict(value))
                real_atomic_json(path, value)
                if value.get("status") == "READY":
                    target.mkdir()

            with patch.object(data_migration, "_atomic_json", side_effect=create_target_before_ready_publish):
                with self.assertRaisesRegex(data_migration.DataMigrationError, "并发创建"):
                    data_migration.ensure_memory_data(source, target_root)

            self.assertTrue(target.is_dir())
            self.assertEqual(list(target.iterdir()), [])
            staging = target_root / data_migration.STAGING_NAME
            self.assertTrue(staging.is_dir())
            preserved = json.loads(
                (staging / data_migration.STATE_FILENAME).read_text(encoding="utf-8")
            )
            self.assertEqual(preserved["status"], "READY")
            states_by_status = {state["status"]: state for state in written_states}
            self.assertEqual(set(states_by_status), {"COPYING", "VERIFYING", "READY"})
            expected_identity = self._staging_identity(source, target_root)
            for state in states_by_status.values():
                for key, expected in expected_identity.items():
                    self.assertEqual(state[key], expected)
            self.assertEqual(preserved, states_by_status["READY"])
            self.assertFalse((staging / data_migration.SOURCE_SNAPSHOT_NAME).exists())

            target.rmdir()
            recovered = data_migration.ensure_memory_data(source, target_root)
            self.assertTrue(recovered.migrated)
            self.assertEqual(recovered.state, preserved)
            self.assertFalse(staging.exists())
            self.assertEqual((target / "payload.txt").read_text(encoding="utf-8"), "source")

    def test_publish_never_replaces_preexisting_empty_directory_or_dangling_symlink(self) -> None:
        for target_kind in ("directory", "symlink"):
            with self.subTest(target_kind=target_kind), tempfile.TemporaryDirectory(dir="temp") as directory:
                root = Path(directory)
                source = root / "legacy"
                target_root = root / "current"
                source.mkdir()
                target_root.mkdir()
                (source / "payload.txt").write_text("source", encoding="utf-8")
                target = target_root / "memory"
                if target_kind == "directory":
                    target.mkdir()
                else:
                    target.symlink_to(root / "missing-target", target_is_directory=True)

                with self.assertRaises(data_migration.DataMigrationError):
                    data_migration.ensure_memory_data(source, target_root)

                if target_kind == "directory":
                    self.assertTrue(target.is_dir())
                    self.assertEqual(list(target.iterdir()), [])
                else:
                    self.assertTrue(target.is_symlink())
                    self.assertEqual(os.readlink(target), str(root / "missing-target"))

    def test_ready_target_symlinks_are_rejected_without_following_or_modifying_them(self) -> None:
        for state_format in ("legacy", "new"):
            with self.subTest(state_format=state_format), tempfile.TemporaryDirectory(dir="temp") as directory:
                root = Path(directory)
                source = root / "legacy"
                external_root = root / "external-root"
                target_root = root / "current"
                source.mkdir()
                (source / "payload.txt").write_bytes(b"external-ready-payload")
                external = data_migration.ensure_memory_data(source, external_root).target_dir
                state_path = external / data_migration.STATE_FILENAME
                if state_format == "legacy":
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                    legacy = {
                        "source": state["source"],
                        "source_present": state["source_present"],
                        "status": state["status"],
                        "verification": state["verification"],
                    }
                    state_path.write_text(json.dumps(legacy), encoding="utf-8")
                external_bytes = {
                    path.relative_to(external).as_posix(): path.read_bytes()
                    for path in sorted(external.rglob("*"))
                    if path.is_file()
                }
                target_root.mkdir()
                target = target_root / "memory"
                target.symlink_to(external, target_is_directory=True)

                with patch.object(
                    data_migration,
                    "_load_state",
                    side_effect=AssertionError("不得读取符号链接目标状态"),
                ):
                    with self.assertRaisesRegex(data_migration.DataMigrationError, "符号链接"):
                        data_migration.ensure_memory_data(source, target_root)

                self.assertTrue(target.is_symlink())
                self.assertEqual(os.readlink(target), str(external))
                self.assertEqual(
                    {
                        path.relative_to(external).as_posix(): path.read_bytes()
                        for path in sorted(external.rglob("*"))
                        if path.is_file()
                    },
                    external_bytes,
                )

    def test_dangling_target_symlink_is_rejected_and_preserved(self) -> None:
        with tempfile.TemporaryDirectory(dir="temp") as directory:
            root = Path(directory)
            source = root / "legacy"
            target_root = root / "current"
            source.mkdir()
            target_root.mkdir()
            missing = root / "missing-memory"
            target = target_root / "memory"
            target.symlink_to(missing, target_is_directory=True)

            with patch.object(
                data_migration,
                "_load_state",
                side_effect=AssertionError("不得读取符号链接目标状态"),
            ):
                with self.assertRaisesRegex(data_migration.DataMigrationError, "符号链接"):
                    data_migration.ensure_memory_data(source, target_root)

            self.assertTrue(target.is_symlink())
            self.assertEqual(os.readlink(target), str(missing))
            self.assertFalse((target_root / data_migration.STAGING_NAME).exists())

    def test_staging_symlinks_are_rejected_without_following_or_deleting_them(self) -> None:
        for staging_kind in ("valid", "dangling"):
            with self.subTest(staging_kind=staging_kind), tempfile.TemporaryDirectory(dir="temp") as directory:
                root = Path(directory)
                source = root / "legacy"
                target_root = root / "current"
                source.mkdir()
                target_root.mkdir()
                staging = target_root / data_migration.STAGING_NAME
                external = root / "external-staging"
                if staging_kind == "valid":
                    external.mkdir()
                    (external / data_migration.STATE_FILENAME).write_bytes(b"external-state")
                    (external / "payload.bin").write_bytes(b"external-payload")
                    external_bytes = {
                        path.relative_to(external).as_posix(): path.read_bytes()
                        for path in sorted(external.rglob("*"))
                        if path.is_file()
                    }
                else:
                    external_bytes = {}
                staging.symlink_to(external, target_is_directory=True)

                with patch.object(
                    data_migration,
                    "_load_state",
                    side_effect=AssertionError("不得读取符号链接暂存状态"),
                ):
                    with self.assertRaisesRegex(data_migration.DataMigrationError, "符号链接"):
                        data_migration.ensure_memory_data(source, target_root)

                self.assertTrue(staging.is_symlink())
                self.assertEqual(os.readlink(staging), str(external))
                self.assertFalse((target_root / "memory").exists())
                if staging_kind == "valid":
                    self.assertEqual(
                        {
                            path.relative_to(external).as_posix(): path.read_bytes()
                            for path in sorted(external.rglob("*"))
                            if path.is_file()
                        },
                        external_bytes,
                    )
                else:
                    self.assertFalse(external.exists())

    def test_corrupt_wal_header_fails_without_changing_source_or_publishing(self) -> None:
        with tempfile.TemporaryDirectory(dir="temp") as directory:
            root = Path(directory)
            source = root / "legacy"
            target_root = root / "current"
            source.mkdir()
            db_path = source / "livingmemory.db"
            _create_crashed_wal_db(db_path, 3)
            wal_path = Path(f"{db_path}-wal")
            damaged = bytearray(wal_path.read_bytes())
            damaged[16] ^= 0xFF
            wal_path.write_bytes(damaged)
            source_manifest = _source_file_snapshot(source)

            with self.assertRaisesRegex(
                data_migration.DataMigrationError,
                "WAL 头损坏",
            ):
                data_migration.ensure_memory_data(source, target_root)

            self.assertEqual(_source_file_snapshot(source), source_manifest)
            self.assertFalse((target_root / "memory").exists())
            state_path = target_root / data_migration.STAGING_NAME / data_migration.STATE_FILENAME
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "FAILED")
            self.assertNotEqual(state["status"], "READY")

    def test_read_only_source_retries_after_one_copy_change_and_publishes_stable_snapshot(self) -> None:
        with tempfile.TemporaryDirectory(dir="temp") as directory:
            root = Path(directory)
            source = root / "legacy"
            target_root = root / "current"
            source.mkdir()
            payload = source / "stable-after-retry.bin"
            payload.write_bytes(b"r" * (8 * 1024 * 1024))
            original_hash = hashlib.sha256(payload.read_bytes()).hexdigest()
            original_mtime = payload.stat().st_mtime_ns
            payload.chmod(0o444)
            source.chmod(0o555)
            copied_payload = (
                target_root
                / data_migration.STAGING_NAME
                / data_migration.SOURCE_SNAPSHOT_NAME
                / payload.name
            )
            marker_path = root / "changed-once"
            updater_script = """
import os
import sys
import time
from pathlib import Path

payload = Path(sys.argv[1])
copied_payload = Path(sys.argv[2])
marker_path = Path(sys.argv[3])
deadline = time.monotonic() + 10
while not copied_payload.exists() and time.monotonic() < deadline:
    time.sleep(0.0005)
if copied_payload.exists():
    stat = payload.stat()
    os.utime(payload, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1))
    marker_path.touch()
"""
            updater = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    updater_script,
                    str(payload),
                    str(copied_payload),
                    str(marker_path),
                ]
            )
            try:
                result = data_migration.ensure_memory_data(source, target_root)
            finally:
                updater.wait(timeout=10)
                source.chmod(0o755)
                payload.chmod(0o644)

            self.assertTrue(marker_path.exists())
            self.assertNotEqual(payload.stat().st_mtime_ns, original_mtime)
            self.assertEqual(hashlib.sha256(payload.read_bytes()).hexdigest(), original_hash)
            self.assertTrue(result.migrated)
            self.assertEqual(result.state["status"], "READY")
            target_payload = result.target_dir / payload.name
            self.assertEqual(hashlib.sha256(target_payload.read_bytes()).hexdigest(), original_hash)
            self.assertEqual(
                result.state["verification"]["hashes"],
                {payload.name: original_hash},
            )

    def test_source_changing_during_copy_never_publishes_ready_target(self) -> None:
        with tempfile.TemporaryDirectory(dir="temp") as directory:
            root = Path(directory)
            source = root / "legacy"
            target_root = root / "current"
            source.mkdir()
            payload = source / "changing.bin"
            payload.write_bytes(b"x" * (32 * 1024 * 1024))
            original_size = payload.stat().st_size
            original_hash = hashlib.sha256(payload.read_bytes()).hexdigest()
            original_mtime = payload.stat().st_mtime_ns
            staging = target_root / data_migration.STAGING_NAME
            stop_path = root / "stop-updater"
            marker_path = root / "updater-ran"
            updater_script = """
import os
import sys
import time
from pathlib import Path

payload = Path(sys.argv[1])
staging = Path(sys.argv[2])
stop_path = Path(sys.argv[3])
marker_path = Path(sys.argv[4])
deadline = time.monotonic() + 10
while not staging.exists() and time.monotonic() < deadline:
    time.sleep(0.0005)
while not stop_path.exists() and time.monotonic() < deadline:
    stat = payload.stat()
    os.utime(payload, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1))
    marker_path.touch()
    time.sleep(0.0005)
"""
            updater = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    updater_script,
                    str(payload),
                    str(staging),
                    str(stop_path),
                    str(marker_path),
                ]
            )
            real_copy_plain_tree = data_migration._copy_plain_tree

            def copy_after_updater_started(source_dir, target_dir):
                deadline = time.monotonic() + 10
                while not marker_path.exists() and time.monotonic() < deadline:
                    time.sleep(0.0005)
                real_copy_plain_tree(source_dir, target_dir)

            try:
                with patch.object(
                    data_migration,
                    "_copy_plain_tree",
                    side_effect=copy_after_updater_started,
                ):
                    with self.assertRaisesRegex(
                        data_migration.DataMigrationError,
                        "无法取得稳定的源目录副本",
                    ):
                        data_migration.ensure_memory_data(source, target_root)
            finally:
                stop_path.touch()
                updater.wait(timeout=10)

            self.assertTrue(marker_path.exists())
            self.assertEqual(payload.stat().st_size, original_size)
            self.assertEqual(hashlib.sha256(payload.read_bytes()).hexdigest(), original_hash)
            self.assertNotEqual(payload.stat().st_mtime_ns, original_mtime)
            self.assertFalse((target_root / "memory").exists())
            state_path = staging / data_migration.STATE_FILENAME
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "FAILED")
            self.assertNotEqual(state["status"], "READY")

    def test_sqlite_backup_includes_nonempty_wal_without_copying_sidecars(self) -> None:
        with tempfile.TemporaryDirectory(dir="temp") as directory:
            root = Path(directory)
            source = root / "legacy"
            target_root = root / "current"
            source.mkdir()
            db_path = source / "livingmemory.db"
            connection = sqlite3.connect(db_path)
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA wal_autocheckpoint=0")
                connection.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY, content TEXT)")
                connection.execute("CREATE TABLE conversations (id INTEGER PRIMARY KEY)")
                connection.commit()
                connection.executemany(
                    "INSERT INTO documents(content) VALUES (?)",
                    [("一",), ("二",), ("三",)],
                )
                connection.execute("INSERT INTO conversations DEFAULT VALUES")
                connection.commit()
                connection.execute("INSERT INTO documents(content) VALUES ('未提交')")

                wal_path = Path(f"{db_path}-wal")
                shm_path = Path(f"{db_path}-shm")
                self.assertTrue(wal_path.is_file())
                self.assertGreater(wal_path.stat().st_size, 0)
                self.assertTrue(shm_path.is_file())

                original_modes = {
                    path: path.stat().st_mode
                    for path in (source, db_path, wal_path, shm_path)
                }
                for path in (db_path, wal_path, shm_path):
                    path.chmod(0o444)
                source.chmod(0o555)
                source_snapshot = _source_file_snapshot(source)

                try:
                    result = data_migration.ensure_memory_data(source, target_root)
                    self.assertEqual(_source_file_snapshot(source), source_snapshot)
                finally:
                    source.chmod(0o755)
                    for path, mode in original_modes.items():
                        if path != source and path.exists():
                            path.chmod(mode)

                self.assertTrue(result.migrated)
                target_db = result.target_dir / "livingmemory.db"
                self.assertTrue(target_db.is_file())
                self.assertFalse(Path(f"{target_db}-wal").exists())
                self.assertFalse(Path(f"{target_db}-shm").exists())
                with closing(sqlite3.connect(target_db)) as migrated:
                    self.assertEqual(
                        migrated.execute("SELECT COUNT(*) FROM documents").fetchone()[0],
                        3,
                    )
                    self.assertEqual(
                        migrated.execute("SELECT COUNT(*) FROM conversations").fetchone()[0],
                        1,
                    )
                report = result.state["verification"]["sqlite"]["livingmemory.db"]
                self.assertEqual(report["counts"], {"documents": 3, "conversations": 1})
                self.assertEqual(
                    report["snapshot_counts"],
                    {"documents": 3, "conversations": 1},
                )
                self.assertNotIn("livingmemory.db-wal", result.state["verification"]["hashes"])
                self.assertNotIn("livingmemory.db-shm", result.state["verification"]["hashes"])

                second = data_migration.ensure_memory_data(source, target_root)
                self.assertFalse(second.migrated)
                self.assertEqual(second.state["status"], "READY")
            finally:
                connection.close()

    def test_source_sqlite_is_never_opened_and_target_validation_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory(dir="temp") as directory:
            root = Path(directory)
            source = root / "legacy"
            target_root = root / "current"
            source.mkdir()
            db_path = source / "livingmemory.db"
            with closing(sqlite3.connect(db_path)) as db, db:
                db.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY)")
                db.execute("INSERT INTO documents DEFAULT VALUES")

            source_snapshot = _source_file_snapshot(source)
            real_connect = sqlite3.connect
            source_calls = []
            snapshot_calls = []
            target_validation_calls = []
            opened_connections = []
            all_calls = []

            def tracked_connect(database, *args, **kwargs):
                database_name = str(database)
                all_calls.append(database_name)
                is_uri = kwargs.get("uri", False)
                if str(db_path.resolve()) in database_name:
                    source_calls.append((database_name, is_uri))
                if data_migration.SOURCE_SNAPSHOT_NAME in database_name:
                    snapshot_calls.append((database_name, is_uri))
                if (
                    data_migration.STAGING_NAME in database_name
                    and data_migration.SOURCE_SNAPSHOT_NAME not in database_name
                    and is_uri
                ):
                    target_validation_calls.append(database_name)
                connection = real_connect(database, *args, **kwargs)
                opened_connections.append(connection)
                return connection

            with patch.object(data_migration.sqlite3, "connect", side_effect=tracked_connect):
                result = data_migration.ensure_memory_data(source, target_root)

            self.assertEqual(_source_file_snapshot(source), source_snapshot)
            self.assertFalse(Path(f"{db_path}-wal").exists())
            self.assertFalse(Path(f"{db_path}-shm").exists())
            self.assertEqual(source_calls, [])
            self.assertTrue(all_calls)
            self.assertTrue(
                all(data_migration.STAGING_NAME in database for database in all_calls)
            )
            self.assertTrue(snapshot_calls)
            self.assertTrue(
                all(
                    data_migration.SOURCE_SNAPSHOT_NAME in database and not uri
                    for database, uri in snapshot_calls
                )
            )
            self.assertTrue(target_validation_calls)
            self.assertTrue(
                all(
                    "mode=ro" in database and "immutable=1" not in database
                    for database in target_validation_calls
                )
            )
            report = result.state["verification"]["sqlite"]["livingmemory.db"]
            self.assertEqual(report["quick_check"], "ok")
            self.assertEqual(report["integrity_check"], "ok")
            self.assertEqual(report["counts"], {"documents": 1})
            self.assertEqual(report["snapshot_counts"], {"documents": 1})
            for connection in opened_connections:
                with self.assertRaises(sqlite3.ProgrammingError):
                    connection.execute("SELECT 1")

    def test_nested_backup_database_is_migrated_and_manifested(self) -> None:
        with tempfile.TemporaryDirectory(dir="temp") as directory:
            root = Path(directory)
            source = root / "legacy"
            target_root = root / "current"
            backup_dir = source / "backups" / "daily"
            backup_dir.mkdir(parents=True)
            db_path = backup_dir / "memory.db"
            with closing(sqlite3.connect(db_path)) as db, db:
                db.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, content TEXT)")
                db.executemany(
                    "INSERT INTO messages(content) VALUES (?)",
                    [("一",), ("二",)],
                )
            (source / "settings.json").write_text('{"enabled": true}\n', encoding="utf-8")
            source_snapshot = _source_file_snapshot(source)

            result = data_migration.ensure_memory_data(source, target_root)

            self.assertEqual(_source_file_snapshot(source), source_snapshot)
            relative = "backups/daily/memory.db"
            target_db = result.target_dir / relative
            self.assertTrue(target_db.is_file())
            self.assertFalse(Path(f"{target_db}-wal").exists())
            self.assertFalse(Path(f"{target_db}-shm").exists())
            with closing(sqlite3.connect(target_db)) as migrated:
                self.assertEqual(
                    migrated.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
                    2,
                )
            verification = result.state["verification"]
            self.assertEqual(
                verification["hashes"][relative],
                hashlib.sha256(target_db.read_bytes()).hexdigest(),
            )
            self.assertEqual(
                verification["hashes"]["settings.json"],
                hashlib.sha256((result.target_dir / "settings.json").read_bytes()).hexdigest(),
            )
            report = verification["sqlite"][relative]
            self.assertEqual(report["quick_check"], "ok")
            self.assertEqual(report["integrity_check"], "ok")
            self.assertEqual(report["counts"], {"messages": 2})
            self.assertEqual(report["snapshot_counts"], {"messages": 2})
            self.assertEqual(report["sha256"], verification["hashes"][relative])

if __name__ == "__main__":
    unittest.main()
