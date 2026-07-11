from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import aiofiles
from astrbot.api import logger

CURRENT_VERSION = "2.3"
DEFAULT_CONFIG = {
    "version": CURRENT_VERSION,
    "next_id": 1,
    "servers": {},
    "last_cleanup": None,
    "trends": {},
}
AUTO_CLEANUP_DAYS = 10
MAX_HISTORY_POINTS = 168

_PATH_LOCKS: Dict[str, asyncio.Lock] = {}


def default_config() -> Dict[str, Any]:
    """返回互不共享可变对象的默认配置。"""
    return deepcopy(DEFAULT_CONFIG)


def _lock_key(path: str | os.PathLike[str]) -> str:
    return str(Path(path).expanduser().resolve())


@asynccontextmanager
async def _locked_path(path: str | os.PathLike[str]):
    key = _lock_key(path)
    lock = _PATH_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _PATH_LOCKS[key] = lock
    await lock.acquire()
    try:
        yield Path(key)
    finally:
        lock.release()


async def _acquire_path_lock(path: str) -> asyncio.Lock:
    """兼容旧调用方；新代码优先使用 _locked_path。"""
    key = _lock_key(path)
    lock = _PATH_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _PATH_LOCKS[key] = lock
    await lock.acquire()
    return lock


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
    logger.info(f"迁移完成，共迁移 {next_id - 1} 个服务器配置")
    return new_data


def _normalize_data(raw: Any) -> tuple[Dict[str, Any], bool]:
    changed = False
    if not isinstance(raw, dict):
        return default_config(), True
    data = raw
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
    if isinstance(legacy_trend, dict) and legacy_trend.get("server_id"):
        sid = str(legacy_trend["server_id"])
        target = data["trends"].setdefault(sid, {}).setdefault("history", [])
        merged: Dict[int, int] = {}
        for item in [*target, *(legacy_trend.get("history", []) or [])]:
            if not isinstance(item, dict):
                continue
            try:
                merged[int(item.get("ts", 0))] = int(item.get("count", 0))
            except (TypeError, ValueError):
                continue
        data["trends"][sid]["history"] = [
            {"ts": ts, "count": count}
            for ts, count in sorted(merged.items())[-MAX_HISTORY_POINTS:]
            if ts > 0
        ]
        data.pop("trend", None)
        changed = True
    return data, changed


async def _write_json_unlocked(path: Path, new_data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    task = asyncio.current_task()
    task_id = id(task) if task else 0
    tmp_path = path.with_name(
        f"{path.name}.{os.getpid()}.{task_id}.{time.time_ns()}.tmp"
    )
    try:
        payload = json.dumps(new_data, indent=4, ensure_ascii=False)
        async with aiofiles.open(tmp_path, "w", encoding="utf-8") as file:
            await file.write(payload)
            await file.flush()
        os.replace(tmp_path, path)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


async def _backup_corrupt_file_unlocked(path: Path, suffix: str) -> None:
    if not path.exists() or path.stat().st_size <= 0:
        return
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.stem}.{suffix}-{stamp}-{time.time_ns()}{path.suffix}")
    try:
        os.replace(path, backup)
    except PermissionError:
        async with aiofiles.open(path, "rb") as source, aiofiles.open(backup, "wb") as target:
            await target.write(await source.read())
    logger.warning(f"已备份疑似损坏的 JSON 文件: {backup.name}")


async def _read_json_unlocked(path: Path) -> Dict[str, Any]:
    if not path.exists():
        data = default_config()
        await _write_json_unlocked(path, data)
        return data

    try:
        async with aiofiles.open(path, "r", encoding="utf-8") as file:
            content = await file.read()
        if not content or not content.strip():
            await _backup_corrupt_file_unlocked(path, "empty")
            data = default_config()
            await _write_json_unlocked(path, data)
            return data
        raw = json.loads(content)
    except json.JSONDecodeError:
        await _backup_corrupt_file_unlocked(path, "invalid")
        data = default_config()
        await _write_json_unlocked(path, data)
        return data

    data, changed = _normalize_data(raw)
    if changed:
        await _write_json_unlocked(path, data)
    return data


async def write_json(json_path: str, new_data: Dict[str, Any]) -> None:
    try:
        async with _locked_path(json_path) as path:
            await _write_json_unlocked(path, deepcopy(new_data))
    except Exception as exc:
        logger.error(f"写入 JSON 文件失败: {exc}")
        raise IOError(f"写入 JSON 文件失败: {exc}") from exc


async def read_json(json_path: str) -> Dict[str, Any]:
    try:
        async with _locked_path(json_path) as path:
            return deepcopy(await _read_json_unlocked(path))
    except Exception as exc:
        logger.error(f"读取 JSON 文件失败: {exc}")
        raise IOError(f"读取 JSON 文件失败: {exc}") from exc


async def _backup_corrupt_file(json_path: str, suffix: str = "corrupt") -> None:
    async with _locked_path(json_path) as path:
        await _backup_corrupt_file_unlocked(path, suffix)


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


async def add_data(json_path: str, name: str, host: str) -> bool:
    try:
        async with _locked_path(json_path) as path:
            data = await _read_json_unlocked(path)
            servers = data["servers"]
            if get_server_by_name(data, name):
                return False
            if any(
                isinstance(info, dict) and info.get("host") == host
                for info in servers.values()
            ):
                return False

            used_ids = {
                int(server_id)
                for server_id in servers
                if str(server_id).isdigit()
            }
            next_id = max(int(data.get("next_id", 1) or 1), max(used_ids, default=0) + 1)
            server_id = str(next_id)
            now = int(time.time())
            servers[server_id] = {
                "id": next_id,
                "name": name,
                "host": host,
                "created_time": now,
                "last_success_time": now,
                "last_failed_time": None,
                "failed_count": 0,
            }
            data["next_id"] = next_id + 1
            await _write_json_unlocked(path, data)
            return True
    except Exception as exc:
        logger.error(f"添加服务器数据失败: {exc}")
        return False


async def del_data(json_path: str, identifier: str) -> bool:
    try:
        async with _locked_path(json_path) as path:
            data = await _read_json_unlocked(path)
            found = _find_server(data, str(identifier))
            if not found:
                return False
            server_id, _ = found
            data["servers"].pop(server_id, None)
            data.get("trends", {}).pop(server_id, None)
            await _write_json_unlocked(path, data)
            return True
    except Exception as exc:
        logger.error(f"删除服务器数据失败: {exc}")
        return False


async def update_data(
    json_path: str,
    identifier: str,
    new_name: Optional[str] = None,
    new_host: Optional[str] = None,
) -> bool:
    try:
        async with _locked_path(json_path) as path:
            data = await _read_json_unlocked(path)
            found = _find_server(data, str(identifier))
            if not found:
                return False
            server_id, server_info = found
            if new_name is not None and new_name != server_info.get("name"):
                duplicate = get_server_by_name(data, new_name)
                if duplicate and duplicate[0] != server_id:
                    return False
            if new_host is not None and new_host != server_info.get("host"):
                if any(
                    str(sid) != server_id
                    and isinstance(info, dict)
                    and info.get("host") == new_host
                    for sid, info in data["servers"].items()
                ):
                    return False
            if new_name is not None:
                server_info["name"] = new_name
            if new_host is not None:
                server_info["host"] = new_host
            await _write_json_unlocked(path, data)
            return True
    except Exception as exc:
        logger.error(f"更新服务器数据失败: {exc}")
        return False


async def get_all_servers(json_path: str) -> Dict[str, Dict[str, Any]]:
    try:
        return (await read_json(json_path)).get("servers", {})
    except Exception as exc:
        logger.error(f"获取服务器列表失败: {exc}")
        return {}


def _hour_bucket(ts: int) -> int:
    return int(ts // 3600 * 3600)


def _append_trend_inplace(data: Dict[str, Any], server_id: str, ts: int, count: int) -> None:
    trends = data.setdefault("trends", {})
    history = trends.setdefault(str(server_id), {}).setdefault("history", [])
    bucket = _hour_bucket(int(ts))
    point = {"ts": bucket, "count": max(0, int(count))}
    if history and isinstance(history[-1], dict) and _hour_bucket(int(history[-1].get("ts", 0) or 0)) == bucket:
        history[-1] = point
    else:
        history.append(point)
    history[:] = history[-MAX_HISTORY_POINTS:]


async def append_trend_point(json_path: str, server_id: str, ts: int, count: int) -> bool:
    try:
        async with _locked_path(json_path) as path:
            data = await _read_json_unlocked(path)
            if str(server_id) not in data.get("servers", {}):
                return False
            _append_trend_inplace(data, str(server_id), ts, count)
            await _write_json_unlocked(path, data)
            return True
    except Exception as exc:
        logger.error(f"追加柱状图记录失败: {exc}")
        return False


async def get_trend_history(
    json_path: str, server_id: str, hours: int = 24
) -> Optional[List[Dict[str, Any]]]:
    try:
        data = await read_json(json_path)
        history = data.get("trends", {}).get(str(server_id), {}).get("history", [])
        return deepcopy(history[-hours:] if hours > 0 else history)
    except Exception as exc:
        logger.error(f"获取柱状图历史失败: {exc}")
        return None


async def get_all_trend_histories(
    json_path: str, hours: int = 24
) -> Dict[str, List[Dict[str, Any]]]:
    try:
        data = await read_json(json_path)
        result: Dict[str, List[Dict[str, Any]]] = {}
        for server_id, trend in data.get("trends", {}).items():
            history = (trend or {}).get("history", [])
            result[str(server_id)] = deepcopy(history[-hours:] if hours > 0 else history)
        return result
    except Exception as exc:
        logger.error(f"获取所有柱状图历史失败: {exc}")
        return {}


async def update_server_status(json_path: str, identifier: str, success: bool) -> bool:
    try:
        async with _locked_path(json_path) as path:
            data = await _read_json_unlocked(path)
            found = _find_server(data, str(identifier))
            if not found:
                return False
            _, server_info = found
            now = int(time.time())
            if success:
                server_info["last_success_time"] = now
                server_info["failed_count"] = 0
            else:
                server_info["last_failed_time"] = now
                server_info["failed_count"] = (
                    int(server_info.get("failed_count", 0) or 0) + 1
                )
            await _write_json_unlocked(path, data)
            return True
    except Exception as exc:
        logger.error(f"更新服务器状态失败: {exc}")
        return False


def _cleanup_candidates(data: Dict[str, Any], now: Optional[int] = None) -> List[Dict[str, Any]]:
    current_time = int(now or time.time())
    cutoff = current_time - AUTO_CLEANUP_DAYS * 24 * 3600
    trends = data.get("trends", {}) or {}
    candidates: List[Dict[str, Any]] = []
    for server_id, server_info in data.get("servers", {}).items():
        if not isinstance(server_info, dict):
            continue
        last_success = int(server_info.get("last_success_time", 0) or 0)
        history = (trends.get(str(server_id)) or {}).get("history", [])
        latest_trend = 0
        if history and isinstance(history[-1], dict):
            latest_trend = int(history[-1].get("ts", 0) or 0)
        effective = max(last_success, latest_trend)
        if effective < cutoff:
            candidates.append(
                {
                    "id": str(server_id),
                    "name": str(server_info.get("name", "")),
                    "host": str(server_info.get("host", "")),
                    "last_success_time": server_info.get("last_success_time"),
                    "failed_count": int(server_info.get("failed_count", 0) or 0),
                    "effective_last_success_time": effective or None,
                }
            )
    return candidates


async def get_cleanup_candidates(json_path: str) -> List[Dict[str, Any]]:
    """只读返回达到自动清理条件的服务器。"""
    try:
        return _cleanup_candidates(await read_json(json_path))
    except Exception as exc:
        logger.error(f"获取自动清理候选失败: {exc}")
        raise


async def auto_cleanup_servers(json_path: str) -> List[Dict[str, Any]]:
    try:
        async with _locked_path(json_path) as path:
            data = await _read_json_unlocked(path)
            candidates = _cleanup_candidates(data)
            if not candidates:
                return []
            for item in candidates:
                server_id = item["id"]
                data.get("servers", {}).pop(server_id, None)
                data.get("trends", {}).pop(server_id, None)
            data["last_cleanup"] = int(time.time())
            await _write_json_unlocked(path, data)
            return candidates
    except Exception as exc:
        logger.error(f"自动清理服务器失败: {exc}")
        return []


async def get_server_info(
    json_path: str, identifier: str
) -> Optional[Dict[str, Any]]:
    try:
        data = await read_json(json_path)
        found = _find_server(data, str(identifier))
        return deepcopy(found[1]) if found else None
    except Exception as exc:
        logger.error(f"获取服务器信息失败: {exc}")
        return None
