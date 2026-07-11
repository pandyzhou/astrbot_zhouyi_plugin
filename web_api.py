from __future__ import annotations

import asyncio
import re
import time
from copy import deepcopy
from functools import wraps
from pathlib import Path
from typing import Any, Awaitable, Callable

from astrbot.api import logger
from astrbot.api.star import StarTools
from astrbot.api.web import error_response, json_response, request

from .script.get_server_info import get_server_status
from .script.json_operate import (
    AUTO_CLEANUP_DAYS,
    add_data,
    append_trend_point,
    auto_cleanup_servers,
    del_data,
    get_cleanup_candidates,
    read_json,
    update_data,
    update_server_status,
)

PLUGIN_NAME = "astrbot_zhouyi_plugin"
PAGE_API_PREFIX = f"/{PLUGIN_NAME}/page"
DATA_DIR_NAME = "astrbot_mcgetter"

_GROUP_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_HOST_RE = re.compile(r"^[A-Za-z0-9.:-]{1,255}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

StatusFetcher = Callable[[str], Awaitable[dict[str, Any] | None]]
DataDirGetter = Callable[[], Path]
Clock = Callable[[], float]


class ApiProblem(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: int = 400,
        code: str = "INVALID_REQUEST",
        data: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code
        self.data = data or {}


def _api_handler(handler):
    @wraps(handler)
    async def wrapped(self, *args, **kwargs):
        try:
            return await handler(self, *args, **kwargs)
        except ApiProblem as exc:
            return error_response(
                exc.message,
                status_code=exc.status_code,
                data={"code": exc.code, **exc.data},
            )
        except Exception:
            logger.error(
                f"Minecraft Manager Page API 处理失败: {handler.__name__}",
                exc_info=True,
            )
            return error_response(
                "服务器内部错误",
                status_code=500,
                data={"code": "INTERNAL_ERROR"},
            )

    return wrapped


def _success(data: Any, *, status_code: int = 200):
    return json_response(
        {"status": "ok", "data": data},
        status_code=status_code,
    )


def _server_sort_key(item: tuple[str, dict[str, Any]]) -> tuple[int, Any]:
    server_id = str(item[0])
    if server_id.isdigit():
        return 0, int(server_id)
    return 1, server_id


def _group_sort_key(group_id: str) -> tuple[int, Any]:
    if re.fullmatch(r"-?\d+", group_id):
        return 0, int(group_id)
    return 1, group_id


def _find_server_entry(
    data: dict[str, Any], identifier: str
) -> tuple[str, dict[str, Any]] | None:
    servers = data.get("servers", {})
    if not isinstance(servers, dict):
        return None
    if identifier in servers and isinstance(servers[identifier], dict):
        return identifier, servers[identifier]
    for server_id, server_info in servers.items():
        if isinstance(server_info, dict) and server_info.get("name") == identifier:
            return str(server_id), server_info
    return None


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


class McManagerWebApi:
    """Minecraft Manager Plugin Page 的真实后端 API。"""

    def __init__(
        self,
        plugin: Any,
        *,
        data_dir_getter: DataDirGetter | None = None,
        status_fetcher: StatusFetcher | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.plugin = plugin
        self._data_dir_getter = data_dir_getter or (
            lambda: StarTools.get_data_dir(DATA_DIR_NAME)
        )
        self._status_fetcher = status_fetcher or get_server_status
        self._clock = clock or time.time

    def register_routes(self) -> None:
        register = self.plugin.context.register_web_api
        routes = [
            ("/bootstrap", self.bootstrap, ["GET"], "Minecraft Manager bootstrap"),
            ("/servers", self.list_servers, ["GET"], "Minecraft Manager servers"),
            ("/servers/add", self.add_server, ["POST"], "Minecraft Manager add server"),
            (
                "/servers/update",
                self.update_server,
                ["POST"],
                "Minecraft Manager update server",
            ),
            (
                "/servers/delete",
                self.delete_server,
                ["POST"],
                "Minecraft Manager delete server",
            ),
            ("/status", self.refresh_status, ["POST"], "Minecraft Manager status"),
            ("/trends", self.get_trends, ["GET"], "Minecraft Manager trends"),
            (
                "/cleanup",
                self.preview_cleanup,
                ["GET"],
                "Minecraft Manager cleanup preview",
            ),
            (
                "/cleanup",
                self.execute_cleanup,
                ["POST"],
                "Minecraft Manager cleanup execute",
            ),
        ]
        for suffix, handler, methods, description in routes:
            register(
                f"{PAGE_API_PREFIX}{suffix}",
                handler,
                methods,
                description,
            )

    def _group_files(self) -> dict[str, Path]:
        data_dir = Path(self._data_dir_getter()).expanduser()
        if not data_dir.exists() or not data_dir.is_dir():
            return {}
        base = data_dir.resolve()
        result: dict[str, Path] = {}
        for candidate in data_dir.iterdir():
            if candidate.suffix != ".json" or not candidate.is_file():
                continue
            group_id = candidate.stem
            if not _GROUP_ID_RE.fullmatch(group_id):
                continue
            try:
                resolved = candidate.resolve()
                resolved.relative_to(base)
            except (OSError, ValueError):
                continue
            if resolved.parent != base:
                continue
            result[group_id] = resolved
        return result

    def _group_path(self, value: Any) -> tuple[str, Path]:
        if not isinstance(value, str):
            raise ApiProblem("group_id 必须是字符串", code="INVALID_GROUP_ID")
        group_id = value.strip()
        if not _GROUP_ID_RE.fullmatch(group_id):
            raise ApiProblem("group_id 格式无效", code="INVALID_GROUP_ID")
        path = self._group_files().get(group_id)
        if path is None:
            raise ApiProblem(
                "群组不存在",
                status_code=404,
                code="GROUP_NOT_FOUND",
            )
        return group_id, path

    @staticmethod
    def _name(value: Any) -> str:
        if not isinstance(value, str):
            raise ApiProblem("name 必须是字符串", code="INVALID_NAME")
        name = value.strip()
        if not name or len(name) > 64 or _CONTROL_RE.search(name):
            raise ApiProblem(
                "name 必须是 1-64 个不含控制字符的字符",
                code="INVALID_NAME",
            )
        return name

    @staticmethod
    def _host(value: Any) -> str:
        if not isinstance(value, str):
            raise ApiProblem("host 必须是字符串", code="INVALID_HOST")
        host = value.strip()
        if not _HOST_RE.fullmatch(host):
            raise ApiProblem(
                "host 只能包含字母、数字和 .:-，长度为 1-255",
                code="INVALID_HOST",
            )
        return host

    @staticmethod
    def _identifier(value: Any) -> str:
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise ApiProblem(
                "identifier 必须是服务器名称或 ID",
                code="INVALID_IDENTIFIER",
            )
        identifier = str(value).strip()
        if not identifier or len(identifier) > 64 or _CONTROL_RE.search(identifier):
            raise ApiProblem(
                "identifier 格式无效",
                code="INVALID_IDENTIFIER",
            )
        return identifier

    @staticmethod
    def _hours(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise ApiProblem("hours 必须是 1-168 的整数", code="INVALID_HOURS")
        raw = str(value).strip()
        if not re.fullmatch(r"\d{1,3}", raw):
            raise ApiProblem("hours 必须是 1-168 的整数", code="INVALID_HOURS")
        hours = int(raw)
        if not 1 <= hours <= 168:
            raise ApiProblem("hours 必须在 1-168 之间", code="INVALID_HOURS")
        return hours

    @staticmethod
    def _strict_bool(value: Any, field: str, *, default: bool | None = None) -> bool:
        if value is None and default is not None:
            return default
        if not isinstance(value, bool):
            raise ApiProblem(
                f"{field} 必须是布尔值",
                code=f"INVALID_{field.upper()}",
            )
        return value

    @staticmethod
    async def _json_body(
        *,
        allowed: set[str],
        required: set[str],
    ) -> dict[str, Any]:
        payload = await request.json(default=None)
        if not isinstance(payload, dict):
            raise ApiProblem("请求体必须是 JSON 对象", code="INVALID_JSON")
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise ApiProblem(
                "请求包含不允许的字段",
                code="UNSUPPORTED_FIELDS",
                data={"fields": unknown},
            )
        missing = sorted(field for field in required if field not in payload)
        if missing:
            raise ApiProblem(
                "请求缺少必填字段",
                code="MISSING_FIELDS",
                data={"fields": missing},
            )
        return payload

    async def _probe(self, host: str) -> dict[str, Any] | None:
        try:
            result = await self._status_fetcher(host)
        except Exception as exc:
            logger.warning(f"Minecraft 状态探测失败 host={host}: {exc}")
            return None
        return result if isinstance(result, dict) else None

    @staticmethod
    def _server_or_404(
        data: dict[str, Any], identifier: str
    ) -> tuple[str, dict[str, Any]]:
        found = _find_server_entry(data, identifier)
        if found is None:
            raise ApiProblem(
                "服务器不存在",
                status_code=404,
                code="SERVER_NOT_FOUND",
            )
        return found

    @staticmethod
    def _check_duplicate(
        data: dict[str, Any],
        *,
        name: str,
        host: str,
        exclude_id: str | None = None,
    ) -> None:
        for server_id, server_info in data.get("servers", {}).items():
            if not isinstance(server_info, dict) or str(server_id) == exclude_id:
                continue
            if server_info.get("name") == name:
                raise ApiProblem(
                    "已存在同名服务器",
                    status_code=409,
                    code="DUPLICATE_NAME",
                )
            if server_info.get("host") == host:
                raise ApiProblem(
                    "已存在相同地址的服务器",
                    status_code=409,
                    code="DUPLICATE_HOST",
                )

    @_api_handler
    async def bootstrap(self):
        group_ids = sorted(self._group_files(), key=_group_sort_key)
        groups = [{"id": group_id} for group_id in group_ids]
        return _success(
            {
                "groups": groups,
                "selected_group_id": group_ids[0] if group_ids else None,
            }
        )

    @_api_handler
    async def list_servers(self):
        group_id, path = self._group_path(request.query.get("group_id"))
        data = await read_json(str(path))
        return _success(
            {
                "group_id": group_id,
                "servers": deepcopy(data.get("servers", {})),
            }
        )

    @_api_handler
    async def add_server(self):
        payload = await self._json_body(
            allowed={"group_id", "name", "host", "force"},
            required={"group_id", "name", "host"},
        )
        group_id, path = self._group_path(payload["group_id"])
        name = self._name(payload["name"])
        host = self._host(payload["host"])
        force = (
            self._strict_bool(payload["force"], "force")
            if "force" in payload
            else False
        )

        data = await read_json(str(path))
        self._check_duplicate(data, name=name, host=host)
        if not force and await self._probe(host) is None:
            raise ApiProblem(
                "服务器预探测失败；确认地址后可使用 force=true 强制添加",
                status_code=502,
                code="PROBE_FAILED",
            )
        if not await add_data(str(path), name, host):
            raise ApiProblem(
                "服务器名称或地址已存在",
                status_code=409,
                code="SERVER_CONFLICT",
            )

        updated = await read_json(str(path))
        found = next(
            (
                deepcopy(server_info)
                for server_info in updated.get("servers", {}).values()
                if isinstance(server_info, dict)
                and server_info.get("name") == name
                and server_info.get("host") == host
            ),
            None,
        )
        if found is None:
            raise RuntimeError("新增服务器后未能读取新增记录")
        return _success(
            {"group_id": group_id, "server": found},
            status_code=201,
        )

    @_api_handler
    async def update_server(self):
        payload = await self._json_body(
            allowed={"group_id", "identifier", "name", "host"},
            required={"group_id", "identifier"},
        )
        if "name" not in payload and "host" not in payload:
            raise ApiProblem(
                "至少提供 name 或 host 之一",
                code="MISSING_UPDATE_FIELDS",
            )
        group_id, path = self._group_path(payload["group_id"])
        identifier = self._identifier(payload["identifier"])
        data = await read_json(str(path))
        server_id, current = self._server_or_404(data, identifier)
        name = self._name(payload["name"]) if "name" in payload else str(current.get("name", ""))
        host = self._host(payload["host"]) if "host" in payload else str(current.get("host", ""))
        self._check_duplicate(
            data,
            name=name,
            host=host,
            exclude_id=server_id,
        )
        if not await update_data(
            str(path),
            server_id,
            name if "name" in payload else None,
            host if "host" in payload else None,
        ):
            latest = await read_json(str(path))
            if _find_server_entry(latest, server_id) is None:
                raise ApiProblem(
                    "服务器不存在",
                    status_code=404,
                    code="SERVER_NOT_FOUND",
                )
            raise ApiProblem(
                "服务器名称或地址冲突",
                status_code=409,
                code="SERVER_CONFLICT",
            )
        updated = await read_json(str(path))
        _, server = self._server_or_404(updated, server_id)
        return _success(
            {"group_id": group_id, "server": deepcopy(server)}
        )

    @_api_handler
    async def delete_server(self):
        payload = await self._json_body(
            allowed={"group_id", "identifier", "confirm"},
            required={"group_id", "identifier", "confirm"},
        )
        confirm = self._strict_bool(payload["confirm"], "confirm")
        if confirm is not True:
            raise ApiProblem(
                "删除服务器必须显式设置 confirm=true",
                code="CONFIRM_REQUIRED",
            )
        group_id, path = self._group_path(payload["group_id"])
        identifier = self._identifier(payload["identifier"])
        data = await read_json(str(path))
        server_id, server = self._server_or_404(data, identifier)
        trend_existed = server_id in (data.get("trends", {}) or {})
        if not await del_data(str(path), server_id):
            raise RuntimeError("删除服务器失败")
        return _success(
            {
                "group_id": group_id,
                "deleted": True,
                "server": deepcopy(server),
                "trend_cascade_deleted": True,
                "trend_existed": trend_existed,
            }
        )

    @_api_handler
    async def refresh_status(self):
        payload = await self._json_body(
            allowed={"group_id", "identifier"},
            required={"group_id"},
        )
        group_id, path = self._group_path(payload["group_id"])
        data = await read_json(str(path))
        servers = data.get("servers", {})
        if not isinstance(servers, dict):
            servers = {}

        if "identifier" in payload and payload["identifier"] is not None:
            identifier = self._identifier(payload["identifier"])
            selected = [self._server_or_404(data, identifier)]
        else:
            selected = sorted(
                (
                    (str(server_id), server_info)
                    for server_id, server_info in servers.items()
                    if isinstance(server_info, dict)
                ),
                key=_server_sort_key,
            )

        queried_at = int(self._clock())
        probe_results = await asyncio.gather(
            *(self._probe(str(server.get("host", ""))) for _, server in selected)
        )
        response_servers: list[dict[str, Any]] = []
        for (server_id, server), status in zip(selected, probe_results):
            success = status is not None
            if not await update_server_status(str(path), server_id, success):
                raise RuntimeError(f"更新服务器状态失败: {server_id}")

            if success:
                online_count = status.get("plays_online")
                if _is_int(online_count):
                    if not await append_trend_point(
                        str(path),
                        server_id,
                        queried_at,
                        online_count,
                    ):
                        raise RuntimeError(f"追加趋势数据失败: {server_id}")
                players = status.get("players_list")
                players_sample = (
                    [item for item in players if isinstance(item, str)]
                    if isinstance(players, list)
                    else []
                )
                item = {
                    "id": server.get("id", server_id),
                    "name": server.get("name"),
                    "host": server.get("host"),
                    "state": "online",
                    "online": True,
                    "queried_at": queried_at,
                    "latency": status.get("latency") if _is_int(status.get("latency")) else None,
                    "version": status.get("server_version") if isinstance(status.get("server_version"), str) else None,
                    "players_online": online_count if _is_int(online_count) else None,
                    "players_max": status.get("plays_max") if _is_int(status.get("plays_max")) else None,
                    "players_sample": players_sample,
                    "players_sample_complete": False,
                    "icon_base64": status.get("icon_base64") if isinstance(status.get("icon_base64"), str) else None,
                }
            else:
                item = {
                    "id": server.get("id", server_id),
                    "name": server.get("name"),
                    "host": server.get("host"),
                    "state": "unreachable",
                    "online": False,
                    "queried_at": queried_at,
                    "latency": None,
                    "version": None,
                    "players_online": None,
                    "players_max": None,
                    "players_sample": [],
                    "players_sample_complete": False,
                    "icon_base64": None,
                }
            response_servers.append(item)

        return _success(
            {
                "group_id": group_id,
                "queried_at": queried_at,
                "servers": response_servers,
            }
        )

    @_api_handler
    async def get_trends(self):
        group_id, path = self._group_path(request.query.get("group_id"))
        hours = self._hours(request.query.get("hours", "24"))
        data = await read_json(str(path))
        servers = data.get("servers", {})
        if not isinstance(servers, dict):
            servers = {}

        raw_identifier = request.query.get("identifier")
        if raw_identifier is not None:
            selected = [self._server_or_404(data, self._identifier(raw_identifier))]
        else:
            selected = sorted(
                (
                    (str(server_id), server_info)
                    for server_id, server_info in servers.items()
                    if isinstance(server_info, dict)
                ),
                key=_server_sort_key,
            )

        generated_at = int(self._clock())
        current_bucket = generated_at // 3600 * 3600
        cutoff = current_bucket - (hours - 1) * 3600
        trends = data.get("trends", {})
        if not isinstance(trends, dict):
            trends = {}
        result: list[dict[str, Any]] = []
        for server_id, server in selected:
            history = (trends.get(server_id) or {}).get("history", [])
            points: list[dict[str, Any]] = []
            if isinstance(history, list):
                for point in history:
                    if not isinstance(point, dict):
                        continue
                    ts = point.get("ts")
                    count = point.get("count")
                    if not _is_int(ts) or not _is_int(count):
                        continue
                    if cutoff <= ts <= current_bucket:
                        points.append(deepcopy(point))
            counts = [point["count"] for point in points]
            result.append(
                {
                    "server": deepcopy(server),
                    "points": points,
                    "latest": counts[-1] if counts else None,
                    "max": max(counts) if counts else None,
                    "average": round(sum(counts) / len(counts), 2) if counts else None,
                    "count": len(counts),
                }
            )

        return _success(
            {
                "group_id": group_id,
                "hours": hours,
                "generated_at": generated_at,
                "servers": result,
            }
        )

    @_api_handler
    async def preview_cleanup(self):
        group_id, path = self._group_path(request.query.get("group_id"))
        candidates = await get_cleanup_candidates(str(path))
        return _success(
            {
                "group_id": group_id,
                "cleanup_days": AUTO_CLEANUP_DAYS,
                "candidates": candidates,
            }
        )

    @_api_handler
    async def execute_cleanup(self):
        payload = await self._json_body(
            allowed={"group_id", "confirm"},
            required={"group_id", "confirm"},
        )
        confirm = self._strict_bool(payload["confirm"], "confirm")
        if confirm is not True:
            raise ApiProblem(
                "执行清理必须显式设置 confirm=true",
                code="CONFIRM_REQUIRED",
            )
        group_id, path = self._group_path(payload["group_id"])
        deleted = await auto_cleanup_servers(str(path))
        return _success(
            {
                "group_id": group_id,
                "cleanup_days": AUTO_CLEANUP_DAYS,
                "deleted": deleted,
                "deleted_count": len(deleted),
            }
        )
