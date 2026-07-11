from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import threading
import time
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from astrbot.api import logger

from .runtime_settings import (
    SCHEMA_VERSION,
    get_effective_settings_sync,
    migrate_schema_v1_to_v2,
)

CURRENT_VERSION = "2.3"
DATABASE_NAME = "mc_manager.sqlite3"
DEFAULT_CONFIG = {
    "version": CURRENT_VERSION,
    "next_id": 1,
    "servers": {},
    "last_cleanup": None,
    "trends": {},
}
AUTO_CLEANUP_DAYS = 10
MAX_HISTORY_POINTS = 10000

_GROUP_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_DB_LOCKS: Dict[str, threading.RLock] = {}
_DB_LOCKS_GUARD = threading.Lock()


@dataclass(frozen=True)
class GroupStorage:
    db_path: Path
    group_id: str


def default_config() -> Dict[str, Any]:
    """返回互不共享可变对象的默认配置。"""
    return deepcopy(DEFAULT_CONFIG)


def get_group_storage(data_dir: str | os.PathLike[str], group_id: str) -> GroupStorage:
    """校验群号，并返回该数据目录内的共享 SQLite 存储定位。"""
    group_id = str(group_id)
    if not _GROUP_ID_RE.fullmatch(group_id):
        raise ValueError("群组 ID 只能包含字母、数字、下划线和连字符，长度为 1-128")
    base = Path(data_dir).expanduser().resolve()
    db_path = (base / DATABASE_NAME).resolve()
    if db_path.parent != base:
        raise ValueError("数据库路径越出数据目录")
    return GroupStorage(db_path=db_path, group_id=group_id)


def _coerce_storage(storage: GroupStorage | str | os.PathLike[str]) -> GroupStorage:
    if isinstance(storage, GroupStorage):
        if not _GROUP_ID_RE.fullmatch(str(storage.group_id)):
            raise ValueError("无效的群组 ID")
        return GroupStorage(Path(storage.db_path).expanduser().resolve(), str(storage.group_id))
    legacy_path = Path(storage).expanduser()
    if legacy_path.suffix.lower() != ".json" or legacy_path.name != f"{legacy_path.stem}.json":
        raise ValueError("兼容路径必须是 <group_id>.json")
    return get_group_storage(legacy_path.parent, legacy_path.stem)


def _legacy_path(storage: GroupStorage | str | os.PathLike[str]) -> Optional[Path]:
    if isinstance(storage, GroupStorage):
        return None
    path = Path(storage).expanduser()
    return path.absolute()


def _db_lock(db_path: Path) -> threading.RLock:
    key = str(db_path)
    with _DB_LOCKS_GUARD:
        return _DB_LOCKS.setdefault(key, threading.RLock())


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS storage_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    row = conn.execute(
        "SELECT value FROM storage_meta WHERE key='schema_version'"
    ).fetchone()
    if row is not None:
        try:
            version = int(row["value"])
        except (TypeError, ValueError) as exc:
            raise RuntimeError("无效的 SQLite schema_version") from exc
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"数据库 schema_version={version} 高于当前支持版本 {SCHEMA_VERSION}"
            )
    else:
        version = 0

    if version == 0:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS groups (
                group_id TEXT PRIMARY KEY,
                next_id INTEGER NOT NULL DEFAULT 1,
                last_cleanup INTEGER
            );
            CREATE TABLE IF NOT EXISTS servers (
                group_id TEXT NOT NULL,
                server_id TEXT NOT NULL,
                name TEXT NOT NULL,
                host TEXT NOT NULL,
                created_time INTEGER,
                last_success_time INTEGER,
                last_failed_time INTEGER,
                failed_count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (group_id, server_id),
                UNIQUE (group_id, name),
                UNIQUE (group_id, host),
                FOREIGN KEY (group_id) REFERENCES groups(group_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS trend_points (
                group_id TEXT NOT NULL,
                server_id TEXT NOT NULL,
                ts INTEGER NOT NULL,
                count INTEGER NOT NULL,
                PRIMARY KEY (group_id, server_id, ts),
                FOREIGN KEY (group_id, server_id)
                    REFERENCES servers(group_id, server_id) ON DELETE CASCADE
            ) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS legacy_json_migrations (
                group_id TEXT PRIMARY KEY,
                source_path TEXT NOT NULL,
                status TEXT NOT NULL,
                migrated_at INTEGER NOT NULL,
                message TEXT
            );
            """
        )
        conn.execute(
            "INSERT INTO storage_meta(key, value) VALUES('schema_version', '1')"
        )
        version = 1

    if version == 1:
        conn.execute("BEGIN IMMEDIATE")
        try:
            migrate_schema_v1_to_v2(conn)
            conn.execute(
                "UPDATE storage_meta SET value='2' WHERE key='schema_version'"
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def _ensure_schema_sync(db_path: Path) -> None:
    with _db_lock(db_path):
        conn = _connect(db_path)
        try:
            _create_schema(conn)
        finally:
            conn.close()


def is_old_format(data: Dict[str, Any]) -> bool:
    if not data or "version" in data:
        return False
    return any(
        isinstance(value, dict) and "name" in value and "host" in value
        for value in data.values()
    )


def migrate_old_format(data: Dict[str, Any]) -> Dict[str, Any]:
    logger.info("检测到旧版配置格式，开始自动迁移...")
    new_data = default_config()
    next_id = 1
    for server_info in data.values():
        if isinstance(server_info, dict) and "name" in server_info and "host" in server_info:
            new_data["servers"][str(next_id)] = {
                "id": next_id,
                "name": server_info["name"],
                "host": server_info["host"],
            }
            next_id += 1
    new_data["next_id"] = next_id
    return new_data


def _normalize_data(raw: Any) -> tuple[Dict[str, Any], bool]:
    changed = False
    if not isinstance(raw, dict):
        return default_config(), True
    data = deepcopy(raw)
    if is_old_format(data):
        data = migrate_old_format(data)
        changed = True
    defaults = default_config()
    for key, value in defaults.items():
        if key not in data:
            data[key] = value
            changed = True
    if not isinstance(data.get("servers"), dict):
        data["servers"] = {}
        changed = True
    if not isinstance(data.get("trends"), dict):
        data["trends"] = {}
        changed = True
    if data.get("version") != CURRENT_VERSION:
        data["version"] = CURRENT_VERSION
        changed = True

    legacy_trend = data.get("trend")
    if isinstance(legacy_trend, dict) and legacy_trend.get("server_id") is not None:
        sid = str(legacy_trend["server_id"])
        target = data["trends"].setdefault(sid, {}).setdefault("history", [])
        merged: Dict[int, int] = {}
        for item in [*target, *(legacy_trend.get("history", []) or [])]:
            if not isinstance(item, dict):
                continue
            try:
                ts = _hour_bucket(int(item.get("ts", 0)))
                if ts > 0:
                    merged[ts] = max(0, int(item.get("count", 0)))
            except (TypeError, ValueError):
                continue
        data["trends"][sid]["history"] = [
            {"ts": ts, "count": count}
            for ts, count in sorted(merged.items())[-MAX_HISTORY_POINTS:]
        ]
        data.pop("trend", None)
        changed = True
    return data, changed


def _public_id(server_id: str) -> int | str:
    try:
        value = int(server_id)
    except (TypeError, ValueError):
        return server_id
    return value if str(value) == str(server_id) else server_id


def _server_record(server_id: str, info: Dict[str, Any]) -> tuple[Any, ...]:
    return (
        str(server_id),
        str(info.get("name", "")),
        str(info.get("host", "")),
        _optional_int(info.get("created_time")),
        _optional_int(info.get("last_success_time")),
        _optional_int(info.get("last_failed_time")),
        int(info.get("failed_count", 0) or 0),
    )


def _optional_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    return int(value)


def _replace_group_sync(conn: sqlite3.Connection, group_id: str, raw: Any) -> None:
    data, _ = _normalize_data(raw)
    servers = data.get("servers", {})
    next_id = max(1, int(data.get("next_id", 1) or 1))
    numeric_ids = [int(sid) for sid in servers if str(sid).isdigit()]
    next_id = max(next_id, max(numeric_ids, default=0) + 1)
    conn.execute(
        "INSERT INTO groups(group_id, next_id, last_cleanup) VALUES(?, ?, ?) "
        "ON CONFLICT(group_id) DO UPDATE SET next_id=excluded.next_id, last_cleanup=excluded.last_cleanup",
        (group_id, next_id, _optional_int(data.get("last_cleanup"))),
    )
    conn.execute("DELETE FROM servers WHERE group_id=?", (group_id,))
    for server_id, raw_info in servers.items():
        if not isinstance(raw_info, dict):
            continue
        sid, name, host, created, last_success, last_failed, failed_count = _server_record(
            str(server_id), raw_info
        )
        conn.execute(
            "INSERT INTO servers(group_id, server_id, name, host, created_time, "
            "last_success_time, last_failed_time, failed_count) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (group_id, sid, name, host, created, last_success, last_failed, failed_count),
        )
    for server_id, trend in data.get("trends", {}).items():
        sid = str(server_id)
        if sid not in {str(item) for item in servers} or not isinstance(trend, dict):
            continue
        merged: Dict[int, int] = {}
        for point in trend.get("history", []) or []:
            if not isinstance(point, dict):
                continue
            try:
                ts = _hour_bucket(int(point.get("ts", 0)))
                if ts > 0:
                    merged[ts] = max(0, int(point.get("count", 0)))
            except (TypeError, ValueError):
                continue
        conn.executemany(
            "INSERT INTO trend_points(group_id, server_id, ts, count) VALUES(?, ?, ?, ?)",
            [
                (group_id, sid, ts, count)
                for ts, count in sorted(merged.items())[-MAX_HISTORY_POINTS:]
            ],
        )


def _migrate_json_sync(storage: GroupStorage, json_path: Path) -> None:
    conn = _connect(storage.db_path)
    try:
        if conn.execute(
            "SELECT 1 FROM legacy_json_migrations WHERE group_id=?", (storage.group_id,)
        ).fetchone():
            return
    finally:
        conn.close()

    try:
        if not json_path.exists() or not json_path.is_file() or json_path.is_symlink():
            return
        if json_path.resolve().parent != storage.db_path.parent:
            logger.warning(f"跳过不安全的 JSON 迁移路径: {json_path}")
            return
        content = json_path.read_text(encoding="utf-8")
        if not content.strip():
            logger.warning(f"跳过空 JSON 文件，原文件已保留: {json_path.name}")
            return
        raw = json.loads(content)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        logger.warning(f"跳过损坏的 JSON 文件，原文件已保留: {json_path.name}: {exc}")
        return

    conn = _connect(storage.db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute(
            "SELECT 1 FROM legacy_json_migrations WHERE group_id=?", (storage.group_id,)
        ).fetchone():
            conn.commit()
            return
        if conn.execute(
            "SELECT 1 FROM groups WHERE group_id=?", (storage.group_id,)
        ).fetchone():
            conn.execute(
                "INSERT INTO legacy_json_migrations(group_id, source_path, status, migrated_at, message) "
                "VALUES(?, ?, 'skipped', ?, 'group already exists')",
                (storage.group_id, str(json_path), int(time.time())),
            )
            conn.commit()
            return
        _replace_group_sync(conn, storage.group_id, raw)
        conn.execute(
            "INSERT INTO legacy_json_migrations(group_id, source_path, status, migrated_at, message) "
            "VALUES(?, ?, 'migrated', ?, NULL)",
            (storage.group_id, str(json_path), int(time.time())),
        )
        conn.commit()
        logger.info(f"已将群 {storage.group_id} 的 JSON 数据迁移到 SQLite")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _prepare_storage_sync(storage: GroupStorage, json_path: Optional[Path] = None) -> None:
    with _db_lock(storage.db_path):
        _ensure_schema_sync(storage.db_path)
        if json_path is not None:
            _migrate_json_sync(storage, json_path)
        conn = _connect(storage.db_path)
        try:
            exists = conn.execute(
                "SELECT 1 FROM groups WHERE group_id=?", (storage.group_id,)
            ).fetchone()
            if exists is None:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT OR IGNORE INTO groups(group_id, next_id, last_cleanup) VALUES(?, 1, NULL)",
                    (storage.group_id,),
                )
                conn.commit()
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()


def _initialize_storage_sync(data_dir: Path) -> List[GroupStorage]:
    base = data_dir.expanduser().resolve()
    base.mkdir(parents=True, exist_ok=True)
    db_path = base / DATABASE_NAME
    with _db_lock(db_path):
        _ensure_schema_sync(db_path)
        for path in sorted(base.glob("*.json")):
            if not _GROUP_ID_RE.fullmatch(path.stem) or path.is_symlink():
                continue
            storage = get_group_storage(base, path.stem)
            try:
                _migrate_json_sync(storage, path)
            except Exception as exc:
                logger.error(f"迁移群 {path.stem} 的 JSON 失败，继续处理其他群: {exc}")
        conn = _connect(db_path)
        try:
            rows = conn.execute("SELECT group_id FROM groups ORDER BY group_id").fetchall()
            return [get_group_storage(base, row["group_id"]) for row in rows]
        finally:
            conn.close()


async def initialize_storage(data_dir: str | os.PathLike[str]) -> List[GroupStorage]:
    """初始化数据库、幂等迁移合法 JSON，并返回已有群存储。"""
    return await asyncio.to_thread(_initialize_storage_sync, Path(data_dir))


async def list_group_storages(data_dir: str | os.PathLike[str]) -> List[GroupStorage]:
    """返回数据库中的群存储；首次调用也会执行初始化和旧数据迁移。"""
    return await initialize_storage(data_dir)


async def _prepared(storage_arg: GroupStorage | str | os.PathLike[str]) -> GroupStorage:
    storage = _coerce_storage(storage_arg)
    await asyncio.to_thread(_prepare_storage_sync, storage, _legacy_path(storage_arg))
    return storage


def _read_group_sync(storage: GroupStorage) -> Dict[str, Any]:
    conn = _connect(storage.db_path)
    try:
        group = conn.execute(
            "SELECT next_id, last_cleanup FROM groups WHERE group_id=?", (storage.group_id,)
        ).fetchone()
        data = default_config()
        if group is None:
            return data
        data["next_id"] = int(group["next_id"])
        data["last_cleanup"] = group["last_cleanup"]
        for row in conn.execute(
            "SELECT server_id, name, host, created_time, last_success_time, "
            "last_failed_time, failed_count FROM servers WHERE group_id=? ORDER BY server_id",
            (storage.group_id,),
        ):
            sid = str(row["server_id"])
            data["servers"][sid] = {
                "id": _public_id(sid),
                "name": row["name"],
                "host": row["host"],
                "created_time": row["created_time"],
                "last_success_time": row["last_success_time"],
                "last_failed_time": row["last_failed_time"],
                "failed_count": int(row["failed_count"] or 0),
            }
        for row in conn.execute(
            "SELECT server_id, ts, count FROM trend_points WHERE group_id=? ORDER BY server_id, ts",
            (storage.group_id,),
        ):
            sid = str(row["server_id"])
            data["trends"].setdefault(sid, {"history": []})["history"].append(
                {"ts": int(row["ts"]), "count": int(row["count"])}
            )
        return data
    finally:
        conn.close()


async def read_json(storage_arg: GroupStorage | str | os.PathLike[str]) -> Dict[str, Any]:
    try:
        storage = await _prepared(storage_arg)
        return await asyncio.to_thread(_read_group_sync, storage)
    except Exception as exc:
        logger.error(f"读取 SQLite 存储失败: {exc}")
        raise IOError(f"读取 SQLite 存储失败: {exc}") from exc


def _write_group_transaction_sync(storage: GroupStorage, data: Dict[str, Any]) -> None:
    conn = _connect(storage.db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        _replace_group_sync(conn, storage.group_id, deepcopy(data))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


async def write_json(
    storage_arg: GroupStorage | str | os.PathLike[str], new_data: Dict[str, Any]
) -> None:
    try:
        storage = await _prepared(storage_arg)
        await asyncio.to_thread(_write_group_transaction_sync, storage, new_data)
    except Exception as exc:
        logger.error(f"写入 SQLite 存储失败: {exc}")
        raise IOError(f"写入 SQLite 存储失败: {exc}") from exc


def get_server_by_name(
    data: Dict[str, Any], name: str
) -> Optional[Tuple[str, Dict[str, Any]]]:
    for server_id, server_info in data.get("servers", {}).items():
        if isinstance(server_info, dict) and server_info.get("name") == name:
            return str(server_id), server_info
    return None


def _find_server(
    data: Dict[str, Any], identifier: str
) -> Optional[Tuple[str, Dict[str, Any]]]:
    servers = data.get("servers", {})
    key = str(identifier)
    if key in servers and isinstance(servers[key], dict):
        return key, servers[key]
    return get_server_by_name(data, key)


def _find_server_id(conn: sqlite3.Connection, group_id: str, identifier: str) -> Optional[str]:
    row = conn.execute(
        "SELECT server_id FROM servers WHERE group_id=? AND server_id=?",
        (group_id, str(identifier)),
    ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT server_id FROM servers WHERE group_id=? AND name=?",
            (group_id, str(identifier)),
        ).fetchone()
    return str(row["server_id"]) if row else None


def _add_data_sync(storage: GroupStorage, name: str, host: str) -> bool:
    conn = _connect(storage.db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        group = conn.execute(
            "SELECT next_id FROM groups WHERE group_id=?", (storage.group_id,)
        ).fetchone()
        next_id = max(1, int(group["next_id"] if group else 1))
        while conn.execute(
            "SELECT 1 FROM servers WHERE group_id=? AND server_id=?",
            (storage.group_id, str(next_id)),
        ).fetchone():
            next_id += 1
        now = int(time.time())
        conn.execute(
            "INSERT INTO servers(group_id, server_id, name, host, created_time, "
            "last_success_time, last_failed_time, failed_count) VALUES(?, ?, ?, ?, ?, ?, NULL, 0)",
            (storage.group_id, str(next_id), name, host, now, now),
        )
        conn.execute(
            "UPDATE groups SET next_id=? WHERE group_id=?", (next_id + 1, storage.group_id)
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        conn.rollback()
        return False
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


async def add_data(storage_arg: GroupStorage | str | os.PathLike[str], name: str, host: str) -> bool:
    try:
        return await asyncio.to_thread(_add_data_sync, await _prepared(storage_arg), name, host)
    except Exception as exc:
        logger.error(f"添加服务器数据失败: {exc}")
        return False


def _delete_data_sync(storage: GroupStorage, identifier: str) -> bool:
    conn = _connect(storage.db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        server_id = _find_server_id(conn, storage.group_id, identifier)
        if server_id is None:
            conn.rollback()
            return False
        conn.execute(
            "DELETE FROM servers WHERE group_id=? AND server_id=?",
            (storage.group_id, server_id),
        )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


async def del_data(storage_arg: GroupStorage | str | os.PathLike[str], identifier: str) -> bool:
    try:
        return await asyncio.to_thread(_delete_data_sync, await _prepared(storage_arg), str(identifier))
    except Exception as exc:
        logger.error(f"删除服务器数据失败: {exc}")
        return False


def _update_data_sync(
    storage: GroupStorage, identifier: str, new_name: Optional[str], new_host: Optional[str]
) -> bool:
    conn = _connect(storage.db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        server_id = _find_server_id(conn, storage.group_id, identifier)
        if server_id is None:
            conn.rollback()
            return False
        fields: List[str] = []
        values: List[Any] = []
        if new_name is not None:
            fields.append("name=?")
            values.append(new_name)
        if new_host is not None:
            fields.append("host=?")
            values.append(new_host)
        if fields:
            values.extend([storage.group_id, server_id])
            conn.execute(
                f"UPDATE servers SET {', '.join(fields)} WHERE group_id=? AND server_id=?",
                values,
            )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        conn.rollback()
        return False
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


async def update_data(
    storage_arg: GroupStorage | str | os.PathLike[str],
    identifier: str,
    new_name: Optional[str] = None,
    new_host: Optional[str] = None,
) -> bool:
    try:
        storage = await _prepared(storage_arg)
        return await asyncio.to_thread(
            _update_data_sync, storage, str(identifier), new_name, new_host
        )
    except Exception as exc:
        logger.error(f"更新服务器数据失败: {exc}")
        return False


async def get_all_servers(
    storage_arg: GroupStorage | str | os.PathLike[str],
) -> Dict[str, Dict[str, Any]]:
    try:
        return (await read_json(storage_arg)).get("servers", {})
    except Exception as exc:
        logger.error(f"获取服务器列表失败: {exc}")
        return {}


def _hour_bucket(ts: int) -> int:
    return int(ts // 3600 * 3600)


def _append_trend_sync(
    storage: GroupStorage,
    server_id: str,
    ts: int,
    count: int,
    max_history_points: Optional[int],
) -> bool:
    conn = _connect(storage.db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        history_limit = (
            get_effective_settings_sync(conn, storage.group_id).max_history_points
            if max_history_points is None
            else int(max_history_points)
        )
        if not 168 <= history_limit <= 100000:
            raise ValueError("max_history_points 必须在 168-100000 之间")
        if not conn.execute(
            "SELECT 1 FROM servers WHERE group_id=? AND server_id=?",
            (storage.group_id, str(server_id)),
        ).fetchone():
            conn.rollback()
            return False
        conn.execute(
            "INSERT INTO trend_points(group_id, server_id, ts, count) VALUES(?, ?, ?, ?) "
            "ON CONFLICT(group_id, server_id, ts) DO UPDATE SET count=excluded.count",
            (storage.group_id, str(server_id), _hour_bucket(int(ts)), max(0, int(count))),
        )
        conn.execute(
            "DELETE FROM trend_points WHERE group_id=? AND server_id=? AND ts NOT IN ("
            "SELECT ts FROM trend_points WHERE group_id=? AND server_id=? "
            "ORDER BY ts DESC LIMIT ?)",
            (
                storage.group_id,
                str(server_id),
                storage.group_id,
                str(server_id),
                history_limit,
            ),
        )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


async def append_trend_point(
    storage_arg: GroupStorage | str | os.PathLike[str],
    server_id: str,
    ts: int,
    count: int,
    max_history_points: Optional[int] = None,
) -> bool:
    try:
        storage = await _prepared(storage_arg)
        return await asyncio.to_thread(
            _append_trend_sync,
            storage,
            str(server_id),
            int(ts),
            int(count),
            max_history_points,
        )
    except Exception as exc:
        logger.error(f"追加柱状图记录失败: {exc}")
        return False


def _get_trend_sync(storage: GroupStorage, server_id: str, hours: int) -> List[Dict[str, Any]]:
    conn = _connect(storage.db_path)
    try:
        if hours > 0:
            rows = conn.execute(
                "SELECT ts, count FROM (SELECT ts, count FROM trend_points "
                "WHERE group_id=? AND server_id=? ORDER BY ts DESC LIMIT ?) ORDER BY ts",
                (storage.group_id, str(server_id), int(hours)),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT ts, count FROM trend_points WHERE group_id=? AND server_id=? ORDER BY ts",
                (storage.group_id, str(server_id)),
            ).fetchall()
        return [{"ts": int(row["ts"]), "count": int(row["count"])} for row in rows]
    finally:
        conn.close()


async def get_trend_history(
    storage_arg: GroupStorage | str | os.PathLike[str], server_id: str, hours: int = 24
) -> Optional[List[Dict[str, Any]]]:
    try:
        storage = await _prepared(storage_arg)
        return await asyncio.to_thread(_get_trend_sync, storage, str(server_id), int(hours))
    except Exception as exc:
        logger.error(f"获取柱状图历史失败: {exc}")
        return None


def _get_all_trends_sync(storage: GroupStorage, hours: int) -> Dict[str, List[Dict[str, Any]]]:
    conn = _connect(storage.db_path)
    try:
        server_ids = [
            str(row["server_id"])
            for row in conn.execute(
                "SELECT server_id FROM servers WHERE group_id=? ORDER BY server_id",
                (storage.group_id,),
            )
        ]
        return {sid: _get_trend_sync(storage, sid, hours) for sid in server_ids}
    finally:
        conn.close()


async def get_all_trend_histories(
    storage_arg: GroupStorage | str | os.PathLike[str], hours: int = 24
) -> Dict[str, List[Dict[str, Any]]]:
    try:
        storage = await _prepared(storage_arg)
        return await asyncio.to_thread(_get_all_trends_sync, storage, int(hours))
    except Exception as exc:
        logger.error(f"获取所有柱状图历史失败: {exc}")
        return {}


def _update_status_sync(storage: GroupStorage, identifier: str, success: bool) -> bool:
    conn = _connect(storage.db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        server_id = _find_server_id(conn, storage.group_id, identifier)
        if server_id is None:
            conn.rollback()
            return False
        now = int(time.time())
        if success:
            conn.execute(
                "UPDATE servers SET last_success_time=?, failed_count=0 "
                "WHERE group_id=? AND server_id=?",
                (now, storage.group_id, server_id),
            )
        else:
            conn.execute(
                "UPDATE servers SET last_failed_time=?, failed_count=failed_count+1 "
                "WHERE group_id=? AND server_id=?",
                (now, storage.group_id, server_id),
            )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


async def update_server_status(
    storage_arg: GroupStorage | str | os.PathLike[str], identifier: str, success: bool
) -> bool:
    try:
        storage = await _prepared(storage_arg)
        return await asyncio.to_thread(
            _update_status_sync, storage, str(identifier), bool(success)
        )
    except Exception as exc:
        logger.error(f"更新服务器状态失败: {exc}")
        return False


def _cleanup_candidates_sync(
    storage: GroupStorage,
    cleanup_days: Optional[int] = None,
    now: Optional[int] = None,
) -> List[Dict[str, Any]]:
    current_time = int(now or time.time())
    conn = _connect(storage.db_path)
    try:
        days = (
            get_effective_settings_sync(conn, storage.group_id).auto_cleanup_days
            if cleanup_days is None
            else int(cleanup_days)
        )
        if not 1 <= days <= 365:
            raise ValueError("cleanup_days 必须在 1-365 之间")
        cutoff = current_time - days * 24 * 3600
        rows = conn.execute(
            "SELECT s.server_id, s.name, s.host, s.last_success_time, s.failed_count, "
            "MAX(COALESCE(s.last_success_time, 0), COALESCE(MAX(t.ts), 0)) AS effective "
            "FROM servers s LEFT JOIN trend_points t "
            "ON t.group_id=s.group_id AND t.server_id=s.server_id "
            "WHERE s.group_id=? GROUP BY s.group_id, s.server_id "
            "HAVING effective < ? ORDER BY s.server_id",
            (storage.group_id, cutoff),
        ).fetchall()
        return [
            {
                "id": str(row["server_id"]),
                "name": str(row["name"]),
                "host": str(row["host"]),
                "last_success_time": row["last_success_time"],
                "failed_count": int(row["failed_count"] or 0),
                "effective_last_success_time": int(row["effective"]) if row["effective"] else None,
            }
            for row in rows
        ]
    finally:
        conn.close()


async def get_cleanup_candidates(
    storage_arg: GroupStorage | str | os.PathLike[str],
    cleanup_days: Optional[int] = None,
) -> List[Dict[str, Any]]:
    try:
        storage = await _prepared(storage_arg)
        return await asyncio.to_thread(_cleanup_candidates_sync, storage, cleanup_days)
    except Exception as exc:
        logger.error(f"获取自动清理候选失败: {exc}")
        raise


def _auto_cleanup_sync(
    storage: GroupStorage, cleanup_days: Optional[int] = None
) -> List[Dict[str, Any]]:
    conn = _connect(storage.db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        current_time = int(time.time())
        days = (
            get_effective_settings_sync(conn, storage.group_id).auto_cleanup_days
            if cleanup_days is None
            else int(cleanup_days)
        )
        if not 1 <= days <= 365:
            raise ValueError("cleanup_days 必须在 1-365 之间")
        cutoff = current_time - days * 24 * 3600
        rows = conn.execute(
            "SELECT s.server_id, s.name, s.host, s.last_success_time, s.failed_count, "
            "MAX(COALESCE(s.last_success_time, 0), COALESCE(MAX(t.ts), 0)) AS effective "
            "FROM servers s LEFT JOIN trend_points t "
            "ON t.group_id=s.group_id AND t.server_id=s.server_id "
            "WHERE s.group_id=? GROUP BY s.group_id, s.server_id "
            "HAVING effective < ? ORDER BY s.server_id",
            (storage.group_id, cutoff),
        ).fetchall()
        candidates = [
            {
                "id": str(row["server_id"]),
                "name": str(row["name"]),
                "host": str(row["host"]),
                "last_success_time": row["last_success_time"],
                "failed_count": int(row["failed_count"] or 0),
                "effective_last_success_time": int(row["effective"]) if row["effective"] else None,
            }
            for row in rows
        ]
        if not candidates:
            conn.rollback()
            return []
        conn.executemany(
            "DELETE FROM servers WHERE group_id=? AND server_id=?",
            [(storage.group_id, item["id"]) for item in candidates],
        )
        conn.execute(
            "UPDATE groups SET last_cleanup=? WHERE group_id=?",
            (current_time, storage.group_id),
        )
        conn.commit()
        return candidates
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


async def auto_cleanup_servers(
    storage_arg: GroupStorage | str | os.PathLike[str],
    cleanup_days: Optional[int] = None,
) -> List[Dict[str, Any]]:
    try:
        storage = await _prepared(storage_arg)
        return await asyncio.to_thread(_auto_cleanup_sync, storage, cleanup_days)
    except Exception as exc:
        logger.error(f"自动清理服务器失败: {exc}")
        return []


async def get_server_info(
    storage_arg: GroupStorage | str | os.PathLike[str], identifier: str
) -> Optional[Dict[str, Any]]:
    try:
        data = await read_json(storage_arg)
        found = _find_server(data, str(identifier))
        return deepcopy(found[1]) if found else None
    except Exception as exc:
        logger.error(f"获取服务器信息失败: {exc}")
        return None
