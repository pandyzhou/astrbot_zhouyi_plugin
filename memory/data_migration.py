"""旧长期记忆数据目录到根插件 ``memory`` 目录的安全迁移。"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

STATE_FILENAME = ".migration-state.json"
STAGING_NAME = ".memory.staging"
SOURCE_SNAPSHOT_NAME = ".source.snapshot"
SOURCE_COPY_ATTEMPTS = 3
VERIFY_SNAPSHOT_PREFIX = ".memory.verify-"
VERIFY_COPY_ATTEMPTS = 3
MIGRATION_STATE_VERSION = 1
MIGRATION_KIND = "memory-data-migration"
LEGACY_READY_KEYS = frozenset({"source", "source_present", "status", "verification"})
READY_IDENTITY_KEYS = frozenset({"kind", "version", "source", "target", "staging"})
READY_STATE_KEYS = frozenset({"status", "source_present", "verification"}) | READY_IDENTITY_KEYS
CORE_SQLITE_FILENAMES = frozenset(
    {"livingmemory.db", "livingmemory_graph_documents.db", "conversations.db"}
)
SQLITE_SUFFIXES = frozenset({".db", ".sqlite", ".sqlite3"})
AT_FDCWD = -100
RENAME_NOREPLACE = 1
CORE_TABLES = (
    "documents",
    "conversations",
    "messages",
    "graph_entries",
    "graph_nodes",
    "graph_edges",
    "memory_atoms",
)


class DataMigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class MigrationResult:
    target_dir: Path
    migrated: bool
    source_present: bool
    state: dict[str, Any]


@dataclass(frozen=True)
class FileManifestEntry:
    size: int
    sha256: str
    mtime_ns: int


def _atomic_publish_noreplace(staging: Path, target: Path) -> None:
    """使用 Linux renameat2 原子发布，目标已存在时绝不替换。"""
    if sys.platform != "linux":
        raise DataMigrationError("当前平台不支持原子 no-replace 目录发布，拒绝迁移")
    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except (AttributeError, OSError) as exc:
        raise DataMigrationError("当前 libc 不提供 renameat2，拒绝迁移") from exc
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    result = renameat2(
        AT_FDCWD,
        os.fsencode(staging),
        AT_FDCWD,
        os.fsencode(target),
        RENAME_NOREPLACE,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        raise DataMigrationError(f"迁移期间目标目录被并发创建: {target}")
    if error_number in {errno.ENOSYS, errno.EINVAL}:
        raise DataMigrationError("当前 Linux 内核或文件系统不支持原子 no-replace 目录发布，拒绝迁移")
    raise DataMigrationError(
        f"原子发布迁移目录失败: {os.strerror(error_number)} ({error_number})"
    )


def _fsync_directory(directory: Path) -> None:
    if not hasattr(os, "O_DIRECTORY"):
        return
    try:
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as output:
            json.dump(value, output, ensure_ascii=False, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _load_state(directory: Path) -> dict[str, Any] | None:
    state_path = directory / STATE_FILENAME
    if state_path.is_symlink():
        raise DataMigrationError(f"迁移状态文件是符号链接，拒绝访问: {state_path}")
    if not state_path.is_file():
        return None
    value = json.loads(state_path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise DataMigrationError(f"迁移状态文件无效: {state_path}")
    return value


def _is_sqlite(path: Path) -> bool:
    if not path.is_file():
        return False
    with path.open("rb") as source:
        return source.read(16) == b"SQLite format 3\x00"


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_manifest(directory: Path) -> dict[str, FileManifestEntry]:
    manifest: dict[str, FileManifestEntry] = {}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise DataMigrationError(f"数据目录包含符号链接，拒绝迁移: {path}")
        if not path.is_file():
            continue
        before = path.stat()
        sha256 = _hash_file(path)
        after = path.stat()
        if (
            before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
        ):
            raise DataMigrationError(f"读取期间源文件发生变化: {path}")
        manifest[path.relative_to(directory).as_posix()] = FileManifestEntry(
            size=after.st_size,
            sha256=sha256,
            mtime_ns=after.st_mtime_ns,
        )
    return manifest


def _hash_tree(
    directory: Path,
    *,
    ignored: set[str] | None = None,
    ignored_prefixes: set[str] | None = None,
) -> dict[str, str]:
    hashes: dict[str, str] = {}
    ignored = ignored or set()
    ignored_prefixes = ignored_prefixes or set()
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise DataMigrationError(f"数据目录包含符号链接，拒绝迁移: {path}")
        relative = path.relative_to(directory).as_posix()
        if any(relative == prefix or relative.startswith(f"{prefix}/") for prefix in ignored_prefixes):
            continue
        if path.is_file() and path.name != STATE_FILENAME and relative not in ignored:
            hashes[relative] = _hash_file(path)
    return hashes


def _copy_plain_tree(source_dir: Path, target_dir: Path) -> None:
    target_dir.mkdir(parents=False)
    for source in sorted(source_dir.rglob("*")):
        relative = source.relative_to(source_dir)
        target = target_dir / relative
        if source.is_symlink():
            raise DataMigrationError(f"旧数据包含符号链接，拒绝迁移: {source}")
        if source.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


def _copy_stable_source(source_dir: Path, staging_dir: Path) -> tuple[Path, dict[str, FileManifestEntry]]:
    snapshot_dir = staging_dir / SOURCE_SNAPSHOT_NAME
    last_error = "源目录在复制期间持续变化"
    for _attempt in range(SOURCE_COPY_ATTEMPTS):
        if snapshot_dir.exists():
            shutil.rmtree(snapshot_dir)
        try:
            before = _file_manifest(source_dir)
            _copy_plain_tree(source_dir, snapshot_dir)
            after = _file_manifest(source_dir)
            copied = _file_manifest(snapshot_dir)
        except (DataMigrationError, OSError) as exc:
            last_error = str(exc)
            continue
        if before == after == copied:
            return snapshot_dir, before
        last_error = "源目录在复制期间发生变化"
    if snapshot_dir.exists():
        shutil.rmtree(snapshot_dir)
    raise DataMigrationError(f"无法取得稳定的源目录副本（最多 {SOURCE_COPY_ATTEMPTS} 次）: {last_error}")


def _sqlite_uri(path: Path) -> str:
    return f"file:{path.resolve().as_posix()}?mode=ro"


def _sqlite_counts_from_connection(db: sqlite3.Connection) -> dict[str, int]:
    counts: dict[str, int] = {}
    tables = {
        row[0]
        for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    for table in CORE_TABLES:
        if table in tables:
            quoted = table.replace('"', '""')
            counts[table] = int(db.execute(f'SELECT COUNT(*) FROM "{quoted}"').fetchone()[0])
    return counts


def _verify_sqlite(path: Path) -> dict[str, Any]:
    with closing(sqlite3.connect(_sqlite_uri(path), uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        quick = str(db.execute("PRAGMA quick_check").fetchone()[0])
        integrity = str(db.execute("PRAGMA integrity_check").fetchone()[0])
        counts = _sqlite_counts_from_connection(db)
    if quick.lower() != "ok" or integrity.lower() != "ok":
        raise DataMigrationError(f"SQLite 完整性检查失败: {path.name}")
    return {"quick_check": quick, "integrity_check": integrity, "counts": counts}


def _validate_wal_header(database: Path) -> None:
    wal = Path(f"{database}-wal")
    if not wal.is_file() or wal.stat().st_size == 0:
        return
    with wal.open("rb") as source:
        header = source.read(32)
    if len(header) != 32 or header[:4] not in {b"7\x7f\x06\x82", b"7\x7f\x06\x83"}:
        raise DataMigrationError(f"SQLite WAL 头损坏: {wal.name}")
    checksum_order = "little" if header[:4] == b"7\x7f\x06\x82" else "big"
    words = [
        int.from_bytes(header[offset : offset + 4], checksum_order)
        for offset in range(0, 24, 4)
    ]
    checksum_a = 0
    checksum_b = 0
    for offset in range(0, len(words), 2):
        checksum_a = (checksum_a + words[offset] + checksum_b) & 0xFFFFFFFF
        checksum_b = (checksum_b + words[offset + 1] + checksum_a) & 0xFFFFFFFF
    stored_checksum = (
        int.from_bytes(header[24:28], "big"),
        int.from_bytes(header[28:32], "big"),
    )
    if stored_checksum != (checksum_a, checksum_b):
        raise DataMigrationError(f"SQLite WAL 头损坏: {wal.name}")
    page_size = int.from_bytes(header[8:12], "big")
    if page_size < 512 or page_size > 65536 or page_size & (page_size - 1):
        raise DataMigrationError(f"SQLite WAL 页大小无效: {wal.name}")


def _backup_sqlite(source: Path, target: Path) -> dict[str, int]:
    """仅从 staging 原始副本的同一读取事务取得计数并完成备份。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    _validate_wal_header(source)
    source.parent.chmod(source.parent.stat().st_mode | 0o700)
    for copied_file in (source, Path(f"{source}-wal"), Path(f"{source}-shm")):
        if copied_file.exists():
            copied_file.chmod(copied_file.stat().st_mode | 0o600)
    Path(f"{source}-shm").unlink(missing_ok=True)
    try:
        with closing(sqlite3.connect(source)) as source_db:
            source_db.execute("BEGIN")
            snapshot_counts = _sqlite_counts_from_connection(source_db)
            with closing(sqlite3.connect(target)) as target_db:
                source_db.backup(target_db)
                target_db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                target_db.execute("PRAGMA journal_mode=DELETE")
            return snapshot_counts
    finally:
        Path(f"{target}-wal").unlink(missing_ok=True)
        Path(f"{target}-shm").unlink(missing_ok=True)


def _sqlite_files(snapshot_dir: Path) -> set[str]:
    return {
        path.relative_to(snapshot_dir).as_posix()
        for path in sorted(snapshot_dir.rglob("*"))
        if not path.is_symlink() and path.is_file() and _is_sqlite(path)
    }


def _copy_snapshot_to_staging(
    snapshot_dir: Path,
    staging_dir: Path,
    sqlite_sources: set[str],
) -> dict[str, dict[str, int]]:
    sqlite_sidecars = {
        f"{relative}{suffix}"
        for relative in sqlite_sources
        for suffix in ("-wal", "-shm")
    }
    excluded = sqlite_sources | sqlite_sidecars | {STATE_FILENAME}
    sqlite_snapshots: dict[str, dict[str, int]] = {}

    for source in sorted(snapshot_dir.rglob("*")):
        relative = source.relative_to(snapshot_dir)
        relative_name = relative.as_posix()
        target = staging_dir / relative
        if source.is_symlink():
            raise DataMigrationError(f"原始副本包含符号链接，拒绝迁移: {source}")
        if source.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif source.is_file() and relative_name not in excluded:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)

    for relative in sorted(sqlite_sources):
        sqlite_snapshots[relative] = _backup_sqlite(
            snapshot_dir / relative,
            staging_dir / relative,
        )
    return sqlite_snapshots


def _verify_copy(
    staging_dir: Path,
    source_manifest: dict[str, FileManifestEntry],
    sqlite_snapshots: dict[str, dict[str, int]],
) -> dict[str, Any]:
    sqlite_set = set(sqlite_snapshots)
    sqlite_sidecars = {
        f"{relative}{suffix}"
        for relative in sqlite_set
        for suffix in ("-wal", "-shm")
    }
    expected_plain = {
        relative: entry.sha256
        for relative, entry in source_manifest.items()
        if relative not in sqlite_set
        and relative not in sqlite_sidecars
        and relative != STATE_FILENAME
    }
    target_hashes = _hash_tree(
        staging_dir,
        ignored_prefixes={SOURCE_SNAPSHOT_NAME},
    )
    expected_files = set(expected_plain) | sqlite_set
    if set(target_hashes) != expected_files:
        missing = sorted(expected_files - set(target_hashes))
        unexpected = sorted(set(target_hashes) - expected_files)
        details = []
        if missing:
            details.append(f"缺少文件: {', '.join(missing)}")
        if unexpected:
            details.append(f"出现额外文件: {', '.join(unexpected)}")
        raise DataMigrationError("迁移目标文件清单不一致（" + "；".join(details) + "）")
    for relative, source_hash in expected_plain.items():
        if target_hashes[relative] != source_hash:
            raise DataMigrationError(f"文件哈希不一致: {relative}")

    sqlite_report: dict[str, Any] = {}
    for relative, snapshot_counts in sqlite_snapshots.items():
        target_path = staging_dir / relative
        target_report = _verify_sqlite(target_path)
        if target_report["counts"] != snapshot_counts:
            raise DataMigrationError(f"SQLite 核心计数不一致: {relative}")
        sqlite_report[relative] = {
            **target_report,
            "snapshot_counts": snapshot_counts,
            "sha256": target_hashes[relative],
        }
    return {"hashes": target_hashes, "sqlite": sqlite_report}


def _staging_identity(source: Path, target: Path) -> dict[str, Any]:
    return {
        "kind": MIGRATION_KIND,
        "version": MIGRATION_STATE_VERSION,
        "source": str(source),
        "target": str(target),
        "staging": STAGING_NAME,
    }


def _validate_staging_identity(
    staging: Path,
    state: dict[str, Any],
    source: Path,
    target: Path,
) -> None:
    labels = {"source": "来源"}
    for key, expected in _staging_identity(source, target).items():
        if key not in state or state[key] != expected:
            label = labels.get(key, key)
            raise DataMigrationError(
                f"迁移暂存目录{label}不匹配，拒绝清理、重建或发布: {staging}"
            )


def _validate_ready_directory_safety(directory: Path) -> None:
    for internal_name in (SOURCE_SNAPSHOT_NAME, STAGING_NAME):
        if (directory / internal_name).exists():
            raise DataMigrationError(f"READY 迁移目录包含异常内部目录: {directory / internal_name}")
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise DataMigrationError(f"READY 迁移目录包含符号链接，拒绝访问: {path}")


def _valid_count_map(value: Any) -> bool:
    return isinstance(value, dict) and all(
        isinstance(table, str)
        and isinstance(count, int)
        and not isinstance(count, bool)
        for table, count in value.items()
    )


def _validate_ready_state_schema(directory: Path, state: dict[str, Any]) -> None:
    if set(state) != READY_STATE_KEYS:
        raise DataMigrationError(f"READY 迁移状态 schema 无效: {directory}")


def _validate_ready_audit_schema(directory: Path, state: dict[str, Any]) -> dict[str, Any]:
    if state.get("status") != "READY" or not isinstance(state.get("source_present"), bool):
        raise DataMigrationError(f"READY 迁移状态无效: {directory}")
    verification = state.get("verification")
    if not isinstance(verification, dict):
        raise DataMigrationError(f"READY 迁移验证记录无效: {directory}")
    recorded_hashes = verification.get("hashes")
    recorded_sqlite = verification.get("sqlite")
    if (
        set(verification) != {"hashes", "sqlite"}
        or not isinstance(recorded_hashes, dict)
        or not isinstance(recorded_sqlite, dict)
        or any(
            not isinstance(relative, str) or not isinstance(digest, str)
            for relative, digest in recorded_hashes.items()
        )
    ):
        raise DataMigrationError(f"READY 迁移验证记录无效: {directory}")
    for relative, report in recorded_sqlite.items():
        if (
            not isinstance(relative, str)
            or not isinstance(report, dict)
            or set(report)
            != {"quick_check", "integrity_check", "counts", "snapshot_counts", "sha256"}
            or not isinstance(report.get("quick_check"), str)
            or not isinstance(report.get("integrity_check"), str)
            or not _valid_count_map(report.get("counts"))
            or not _valid_count_map(report.get("snapshot_counts"))
            or not isinstance(report.get("sha256"), str)
            or recorded_hashes.get(relative) != report.get("sha256")
        ):
            raise DataMigrationError(f"READY SQLite 验证记录无效: {directory}")
    return verification


def _validate_ready_contents(directory: Path, state: dict[str, Any]) -> dict[str, Any]:
    """严格重新验证尚未发布的 READY staging。"""
    _validate_ready_directory_safety(directory)
    verification = _validate_ready_audit_schema(directory, state)
    recorded_hashes = verification["hashes"]
    recorded_sqlite = verification["sqlite"]

    current_hashes = _hash_tree(directory)
    if current_hashes != recorded_hashes:
        raise DataMigrationError(f"READY 迁移目标文件哈希与验证记录不一致: {directory}")

    current_sqlite_files = _sqlite_files(directory)
    if set(recorded_sqlite) != current_sqlite_files:
        raise DataMigrationError(f"READY SQLite 文件清单与验证记录不一致: {directory}")

    current_sqlite: dict[str, Any] = {}
    for relative, recorded_report in recorded_sqlite.items():
        try:
            target_report = _verify_sqlite(directory / relative)
        except (OSError, sqlite3.DatabaseError) as exc:
            raise DataMigrationError(f"READY SQLite 重新验证失败: {relative}: {exc}") from exc
        snapshot_counts = recorded_report["snapshot_counts"]
        if target_report["counts"] != snapshot_counts:
            raise DataMigrationError(f"READY SQLite 核心计数与验证记录不一致: {relative}")
        current_sqlite[relative] = {
            **target_report,
            "snapshot_counts": snapshot_counts,
            "sha256": current_hashes[relative],
        }

    current_verification = {"hashes": current_hashes, "sqlite": current_sqlite}
    if current_verification != verification:
        raise DataMigrationError(f"READY 迁移重新验证结果与记录不一致: {directory}")
    return verification


def _runtime_sqlite_files(directory: Path) -> set[str]:
    sqlite_files: set[str] = set()
    for path in sorted(directory.rglob("*")):
        if not path.is_file() or path.name == STATE_FILENAME:
            continue
        if path.name.endswith(("-wal", "-shm")):
            continue
        if path.suffix.lower() in SQLITE_SUFFIXES or _is_sqlite(path):
            sqlite_files.add(path.relative_to(directory).as_posix())
    return sqlite_files


def _remove_verify_snapshot(path: Path) -> None:
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        path.unlink(missing_ok=True)
        return
    shutil.rmtree(path)


def _create_verify_snapshot_root(root: Path) -> Path:
    verify_root = Path(tempfile.mkdtemp(dir=root, prefix=VERIFY_SNAPSHOT_PREFIX))
    mode = os.lstat(verify_root).st_mode
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        _remove_verify_snapshot(verify_root)
        raise DataMigrationError(f"READY 私有验证快照路径不安全: {verify_root}")
    return verify_root


def _validate_published_snapshot_contents(
    snapshot: Path,
    verification: dict[str, Any],
) -> None:
    _validate_ready_directory_safety(snapshot)
    for path in sorted(snapshot.rglob("*")):
        if path.is_file() and path.name.endswith("-shm"):
            path.unlink()

    current_sqlite = _runtime_sqlite_files(snapshot)
    expected_core = set(verification["sqlite"]) & set(CORE_SQLITE_FILENAMES)
    missing_core = expected_core - current_sqlite
    if missing_core:
        raise DataMigrationError(
            f"READY 迁移目标缺少预期核心 SQLite: {', '.join(sorted(missing_core))}"
        )
    for relative in sorted(current_sqlite):
        try:
            database = snapshot / relative
            _validate_wal_header(database)
            _verify_sqlite(database)
        except (DataMigrationError, OSError, sqlite3.DatabaseError) as exc:
            if isinstance(exc, DataMigrationError):
                raise
            raise DataMigrationError(f"READY SQLite 运行期完整性验证失败: {relative}: {exc}") from exc


def _validate_published_ready_contents(directory: Path, state: dict[str, Any]) -> dict[str, Any]:
    """在稳定的私有副本内验证已发布 READY 目录，绝不连接运行期目标。"""
    _validate_ready_directory_safety(directory)
    verification = _validate_ready_audit_schema(directory, state)
    verify_root = _create_verify_snapshot_root(directory.parent)
    snapshot = verify_root / "memory"
    last_error = "READY 目标在验证期间持续变化"
    try:
        for _attempt in range(VERIFY_COPY_ATTEMPTS):
            _remove_verify_snapshot(snapshot)
            try:
                before = _file_manifest(directory)
                _copy_plain_tree(directory, snapshot)
                after_copy = _file_manifest(directory)
                copied = _file_manifest(snapshot)
            except (DataMigrationError, OSError) as exc:
                last_error = str(exc)
                continue
            if before != after_copy or before != copied:
                last_error = "READY 目标在复制期间发生变化"
                continue

            try:
                _validate_published_snapshot_contents(snapshot, verification)
            except (DataMigrationError, OSError, sqlite3.DatabaseError):
                try:
                    after_validation = _file_manifest(directory)
                except (DataMigrationError, OSError) as exc:
                    last_error = str(exc)
                    continue
                if after_validation != before:
                    last_error = "READY 目标在验证期间发生变化"
                    continue
                raise

            try:
                after_validation = _file_manifest(directory)
            except (DataMigrationError, OSError) as exc:
                last_error = str(exc)
                continue
            if after_validation == before:
                return verification
            last_error = "READY 目标在验证期间发生变化"
        raise DataMigrationError(
            f"无法取得稳定的 READY 私有验证快照（最多 {VERIFY_COPY_ATTEMPTS} 次）: {last_error}"
        )
    finally:
        _remove_verify_snapshot(verify_root)


def _validate_ready_staging(
    staging: Path,
    state: dict[str, Any],
    source: Path,
    target: Path,
) -> dict[str, Any]:
    _validate_ready_state_schema(staging, state)
    _validate_staging_identity(staging, state, source, target)
    return _validate_ready_contents(staging, state)


def _validate_existing_ready_target(
    target: Path,
    state: dict[str, Any],
    source: Path,
) -> dict[str, Any]:
    sibling_staging = target.parent / STAGING_NAME
    if sibling_staging.exists():
        raise DataMigrationError(f"READY 迁移目标旁存在异常暂存目录: {sibling_staging}")

    if set(state) == LEGACY_READY_KEYS:
        if not isinstance(state.get("source"), str) or state["source"] != str(source):
            raise DataMigrationError(f"READY 迁移目录来源不匹配: {target}")
        _validate_published_ready_contents(target, state)
        upgraded = {**state, **_staging_identity(source, target)}
        try:
            _atomic_json(target / STATE_FILENAME, upgraded)
        except OSError as exc:
            raise DataMigrationError(f"READY legacy 状态原子升级失败: {exc}") from exc
        return upgraded

    _validate_ready_state_schema(target, state)
    _validate_staging_identity(target, state, source, target)
    _validate_published_ready_contents(target, state)
    return state


def _normalized_absolute_path(path: str | os.PathLike[str]) -> Path:
    """仅做词法绝对化，不解析或跟随路径中的符号链接。"""
    return Path(os.path.abspath(os.path.expanduser(os.fspath(path))))


def _paths_overlap(left: Path, right: Path) -> bool:
    try:
        common = Path(os.path.commonpath((left, right)))
    except ValueError:
        return False
    return common == left or common == right


def _reject_overlapping_paths(source: Path, root: Path) -> None:
    try:
        resolved_source = source.resolve(strict=False)
        resolved_root = root.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise DataMigrationError(f"无法解析数据目录真实路径，拒绝迁移: {exc}") from exc
    if _paths_overlap(source, root) or _paths_overlap(resolved_source, resolved_root):
        raise DataMigrationError(f"旧数据目录与根数据目录重叠，拒绝迁移: {source} / {root}")


def _mark_staging_failed(
    staging: Path,
    source: Path,
    target: Path,
    exc: Exception,
) -> None:
    if not staging.is_dir():
        return
    try:
        try:
            existing = _load_state(staging) or {}
        except Exception:
            existing = {}
        failed = {
            **existing,
            **_staging_identity(source, target),
            "status": "FAILED",
            "error": str(exc),
        }
        _atomic_json(staging / STATE_FILENAME, failed)
    except Exception:
        pass


def ensure_memory_data(source_dir: str | os.PathLike[str], root_data_dir: str | os.PathLike[str]) -> MigrationResult:
    """确保 ``root_data_dir/memory`` 为 READY；源目录永不写入或删除。"""
    source = _normalized_absolute_path(source_dir)
    root = _normalized_absolute_path(root_data_dir)
    _reject_overlapping_paths(source, root)
    target = root / "memory"
    staging = root / STAGING_NAME
    root.mkdir(parents=True, exist_ok=True)

    if target.is_symlink():
        raise DataMigrationError(f"目标 Memory 路径是符号链接，拒绝访问: {target}")
    if staging.is_symlink():
        raise DataMigrationError(f"迁移暂存路径是符号链接，拒绝访问: {staging}")

    if target.exists():
        state = _load_state(target)
        if state and state.get("status") == "READY":
            validated_state = _validate_existing_ready_target(target, state, source)
            return MigrationResult(target, False, source.is_dir(), validated_state)
        raise DataMigrationError(f"目标 Memory 目录已存在但未处于 READY 状态: {target}")

    if staging.exists():
        staging_state = _load_state(staging)
        if not staging_state:
            raise DataMigrationError(f"发现来源不明的迁移暂存目录: {staging}")
        status = staging_state.get("status")
        if status not in {"READY", "COPYING", "VERIFYING", "FAILED"}:
            raise DataMigrationError(f"发现来源不明的迁移暂存目录: {staging}")
        if status == "READY":
            try:
                _validate_ready_staging(staging, staging_state, source, target)
                _atomic_publish_noreplace(staging, target)
                _fsync_directory(root)
                return MigrationResult(
                    target,
                    bool(staging_state.get("source_present")),
                    source.is_dir(),
                    staging_state,
                )
            except Exception as exc:
                if isinstance(exc, DataMigrationError):
                    raise
                raise DataMigrationError(f"READY 迁移暂存目录恢复失败: {exc}") from exc
        _validate_staging_identity(staging, staging_state, source, target)
        shutil.rmtree(staging)

    staging.mkdir(parents=False)
    identity = _staging_identity(source, target)
    _atomic_json(staging / STATE_FILENAME, {"status": "COPYING", **identity})
    ready_written = False
    try:
        if source.is_dir():
            snapshot_dir, source_manifest = _copy_stable_source(source, staging)
            sqlite_sources = _sqlite_files(snapshot_dir)
            sqlite_snapshots = _copy_snapshot_to_staging(
                snapshot_dir,
                staging,
                sqlite_sources,
            )
            _atomic_json(
                staging / STATE_FILENAME,
                {
                    "status": "VERIFYING",
                    **identity,
                    "sqlite_files": sorted(sqlite_snapshots),
                },
            )
            report = _verify_copy(staging, source_manifest, sqlite_snapshots)
            shutil.rmtree(snapshot_dir)
        else:
            report = {"hashes": {}, "sqlite": {}}

        ready = {
            "status": "READY",
            **identity,
            "source_present": source.is_dir(),
            "verification": report,
        }
        _atomic_json(staging / STATE_FILENAME, ready)
        ready_written = True
        _atomic_publish_noreplace(staging, target)
        _fsync_directory(root)
        return MigrationResult(target, source.is_dir(), source.is_dir(), ready)
    except Exception as exc:
        if not ready_written:
            _mark_staging_failed(staging, source, target, exc)
        if isinstance(exc, DataMigrationError):
            raise
        raise DataMigrationError(f"长期记忆数据迁移失败: {exc}") from exc
