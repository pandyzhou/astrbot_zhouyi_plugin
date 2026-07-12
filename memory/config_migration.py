"""长期记忆配置迁移；导入模块不会自动修改文件。"""

from __future__ import annotations

import copy
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None  # type: ignore[assignment]

LEGACY_CONFIG_FILENAME = "astrbot_plugin_livingmemory_config.json"
TARGET_CONFIG_FILENAME = "astrbot_zhouyi_plugin_config.json"
TARGET_BACKUP_SUFFIX = ".pre-memory-migration.bak"
TARGET_KEY = "memory"
LEGACY_ROOT_KEY = "living_memory"


def get_config_paths(config_dir: str | os.PathLike[str]) -> tuple[Path, Path]:
    directory = Path(config_dir)
    return directory / LEGACY_CONFIG_FILENAME, directory / TARGET_CONFIG_FILENAME


def _deep_fill(target: dict[str, Any], source: Mapping[str, Any]) -> None:
    """只补齐缺失字段；现有目标值始终优先。"""
    for key, value in source.items():
        if key not in target:
            target[key] = copy.deepcopy(value)
        elif isinstance(target[key], dict) and isinstance(value, Mapping):
            _deep_fill(target[key], value)


def wrap_legacy_config(legacy_config: Mapping[str, Any]) -> dict[str, Any]:
    memory = copy.deepcopy(dict(legacy_config))
    memory.setdefault("enabled", True)
    return {TARGET_KEY: memory}


def migrate_config(config: Mapping[str, Any], legacy_config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """迁移根 ``living_memory`` 与旧独立配置，保留未知字段。"""
    result = copy.deepcopy(dict(config))
    current = result.get(TARGET_KEY)
    old_root = result.get(LEGACY_ROOT_KEY)
    if current is not None and not isinstance(current, dict):
        raise ValueError("memory 配置必须是 JSON 对象")
    if old_root is not None and not isinstance(old_root, dict):
        raise ValueError("旧 living_memory 配置必须是 JSON 对象")
    if legacy_config is not None and not isinstance(legacy_config, Mapping):
        raise ValueError("旧独立长期记忆配置必须是 JSON 对象")

    memory: dict[str, Any] = copy.deepcopy(current) if isinstance(current, dict) else {}
    if isinstance(old_root, dict):
        _deep_fill(memory, old_root)
    if legacy_config is not None:
        _deep_fill(memory, legacy_config)
    if memory or old_root is not None or legacy_config is not None:
        memory.setdefault("enabled", True)
        result[TARGET_KEY] = memory
    result.pop(LEGACY_ROOT_KEY, None)
    return result


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


def _load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"配置根节点必须是 JSON 对象: {path.name}")
    return value


def _serialize(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=4) + "\n").encode("utf-8")


def _write_new_file(target_path: Path, content: bytes) -> bool:
    fd, name = tempfile.mkstemp(dir=target_path.parent, prefix=f".{target_path.name}.", suffix=".tmp")
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temporary, target_path)
        except FileExistsError:
            return False
        _fsync_directory(target_path.parent)
        return True
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_replace(target_path: Path, content: bytes) -> None:
    fd, name = tempfile.mkstemp(
        dir=target_path.parent,
        prefix=f".{target_path.name}.",
        suffix=".tmp",
    )
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target_path)
        _fsync_directory(target_path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _lock_path(target_path: Path) -> Path:
    return target_path.with_name(f".{target_path.name}.migration.lock")


@contextmanager
def _migration_lock(target_path: Path) -> Iterator[None]:
    """跨进程串行化目标配置的完整读改写事务。"""
    lock_path = _lock_path(target_path)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    locked = False
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
            locked = True
        elif msvcrt is not None:  # pragma: no cover - Windows
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
                os.fsync(fd)
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            locked = True
        else:  # pragma: no cover - Python 支持的平台均应提供其一
            raise RuntimeError("当前平台不支持安全的配置文件锁")
        yield
    finally:
        if locked:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
            elif msvcrt is not None:  # pragma: no cover - Windows
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        os.close(fd)


def _backup_path(target_path: Path) -> Path:
    return target_path.with_name(f"{target_path.name}{TARGET_BACKUP_SUFFIX}")


def _ensure_one_time_backup(target_path: Path, original: bytes) -> None:
    """首次修改已有根配置前保存同目录快照，后续迁移不覆盖。"""
    backup_path = _backup_path(target_path)
    if backup_path.exists():
        return
    _write_new_file(backup_path, original)


def migrate_config_file(config_dir: str | os.PathLike[str]) -> bool:
    """在跨进程锁内完成读取、合并、一次性备份和原子替换。"""
    legacy_path, target_path = get_config_paths(config_dir)
    target_path.parent.mkdir(parents=True, exist_ok=True)

    with _migration_lock(target_path):
        legacy = _load_object(legacy_path) if legacy_path.is_file() else None
        if not target_path.exists():
            if legacy is None:
                return False
            _atomic_replace(target_path, _serialize(wrap_legacy_config(legacy)))
            return True

        original = target_path.read_bytes()
        current = json.loads(original.decode("utf-8-sig"))
        if not isinstance(current, dict):
            raise ValueError("根插件配置的根节点必须是 JSON 对象")
        migrated = migrate_config(current, legacy)
        content = _serialize(migrated)
        if content == original:
            return False
        _ensure_one_time_backup(target_path, original)
        _atomic_replace(target_path, content)
        return True
