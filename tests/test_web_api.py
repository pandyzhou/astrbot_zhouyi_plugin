from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlencode

ASTRBOT_ROOT = Path(__file__).resolve().parents[4]
if str(ASTRBOT_ROOT) not in sys.path:
    sys.path.insert(0, str(ASTRBOT_ROOT))

from astrbot.api.web import PluginRequest, bind_request_context
from starlette.requests import Request

from data.plugins.astrbot_zhouyi_plugin.script.json_operate import (
    AUTO_CLEANUP_DAYS,
    GroupStorage,
    default_config,
    get_group_storage,
    read_json,
    write_json,
)
from data.plugins.astrbot_zhouyi_plugin.web_api import (
    DATA_DIR_NAME,
    PAGE_API_PREFIX,
    McManagerWebApi,
)


class DummyContext:
    def __init__(self) -> None:
        self.routes = []

    def register_web_api(self, route, handler, methods, description) -> None:
        for index, current in enumerate(self.routes):
            if current[0] == route and current[2] == methods:
                self.routes[index] = (route, handler, methods, description)
                return
        self.routes.append((route, handler, methods, description))


class DummyPlugin:
    def __init__(self) -> None:
        self.context = DummyContext()


def _server(server_id: int, name: str, host: str, **overrides):
    now = int(time.time())
    value = {
        "id": server_id,
        "name": name,
        "host": host,
        "created_time": now - 3600,
        "last_success_time": now - 3600,
        "last_failed_time": None,
        "failed_count": 0,
    }
    value.update(overrides)
    return value


class McManagerWebApiContractTests(unittest.IsolatedAsyncioTestCase):
    def test_data_dir_name_uses_current_plugin_name(self):
        self.assertEqual(DATA_DIR_NAME, "astrbot_zhouyi_plugin")
        self.assertNotIn(DATA_DIR_NAME, {"astrbot_mcgetter", "astrbot_mcgetter_enhanced"})

    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        self.group_id = "12345"
        self.storage = get_group_storage(self.data_dir, self.group_id)
        self.now = int(time.time())
        self.statuses = {}
        self.probe_calls = []

        async def fake_status(host: str):
            self.probe_calls.append(host)
            value = self.statuses.get(host)
            if isinstance(value, Exception):
                raise value
            return deepcopy(value)

        self.plugin = DummyPlugin()
        self.api = McManagerWebApi(
            self.plugin,
            data_dir_getter=lambda: self.data_dir,
            status_fetcher=fake_status,
            clock=lambda: self.now,
        )
        await self._write_group(
            {
                "1": _server(1, "Alpha", "alpha.example:25565"),
            }
        )

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def _write_group(self, servers, *, trends=None, group_id=None) -> GroupStorage:
        target_group = group_id or self.group_id
        storage = get_group_storage(self.data_dir, target_group)
        data = default_config()
        data["servers"] = deepcopy(servers)
        numeric_ids = [int(item) for item in servers if str(item).isdigit()]
        data["next_id"] = max(numeric_ids, default=0) + 1
        data["trends"] = deepcopy(trends or {})
        await write_json(storage, data)
        return storage

    async def _call(self, handler, *, method="GET", query=None, body=None):
        query_string = urlencode(query or {}).encode("utf-8")
        raw_body = (
            json.dumps(body, ensure_ascii=False).encode("utf-8")
            if body is not None
            else b""
        )
        headers = []
        if body is not None:
            headers.append((b"content-type", b"application/json"))
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": "/api/v1/plugins/extensions/test",
            "raw_path": b"/api/v1/plugins/extensions/test",
            "query_string": query_string,
            "headers": headers,
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        }
        delivered = False

        async def receive():
            nonlocal delivered
            if delivered:
                return {"type": "http.disconnect"}
            delivered = True
            return {"type": "http.request", "body": raw_body, "more_body": False}

        starlette_request = Request(scope, receive)
        plugin_request = PluginRequest(
            starlette_request,
            plugin_name="astrbot_zhouyi_plugin",
            username="tester",
        )
        with bind_request_context(plugin_request):
            response = await handler()
        payload = json.loads(response.body.decode("utf-8"))
        return response.status_code, payload

    @staticmethod
    def _frontend_unwrap(payload):
        if payload.get("status") == "ok":
            return payload["data"]
        if isinstance(payload.get("success"), bool):
            if payload["success"]:
                return payload["data"]
            raise AssertionError(payload)
        raise AssertionError(f"前端无法识别响应: {payload!r}")

    async def test_bridge_and_direct_fetch_success_shapes(self):
        status, payload = await self._call(self.api.bootstrap)
        self.assertEqual(status, 200)

        direct_data = self._frontend_unwrap(payload)
        bridge_data = payload["data"]
        expected = {
            "groups": [{"id": self.group_id}],
            "selected_group_id": self.group_id,
        }
        self.assertEqual(direct_data, expected)
        self.assertEqual(bridge_data, expected)

    async def test_route_registration_contract(self):
        self.api.register_routes()
        self.api.register_routes()
        self.assertEqual(len(self.plugin.context.routes), 9)
        registered = {(route, tuple(methods)) for route, _, methods, _ in self.plugin.context.routes}
        self.assertEqual(
            registered,
            {
                (f"{PAGE_API_PREFIX}/bootstrap", ("GET",)),
                (f"{PAGE_API_PREFIX}/servers", ("GET",)),
                (f"{PAGE_API_PREFIX}/servers/add", ("POST",)),
                (f"{PAGE_API_PREFIX}/servers/update", ("POST",)),
                (f"{PAGE_API_PREFIX}/servers/delete", ("POST",)),
                (f"{PAGE_API_PREFIX}/status", ("POST",)),
                (f"{PAGE_API_PREFIX}/trends", ("GET",)),
                (f"{PAGE_API_PREFIX}/cleanup", ("GET",)),
                (f"{PAGE_API_PREFIX}/cleanup", ("POST",)),
            },
        )

    async def test_group_whitelist_bootstrap_and_server_list(self):
        status, payload = await self._call(self.api.bootstrap)
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["data"]["groups"], [{"id": self.group_id}])
        self.assertEqual(payload["data"]["selected_group_id"], self.group_id)

        status, payload = await self._call(
            self.api.list_servers,
            query={"group_id": self.group_id},
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            payload["data"]["servers"]["1"],
            (await read_json(self.storage))["servers"]["1"],
        )

        status, payload = await self._call(
            self.api.list_servers,
            query={"group_id": "../12345"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["data"]["code"], "INVALID_GROUP_ID")

        status, payload = await self._call(
            self.api.list_servers,
            query={"group_id": "99999"},
        )
        self.assertEqual(status, 404)
        self.assertEqual(payload["data"]["code"], "GROUP_NOT_FOUND")

    async def test_add_force_and_probe_failure(self):
        host = "offline.example:25565"
        self.statuses[host] = None

        status, payload = await self._call(
            self.api.add_server,
            method="POST",
            body={
                "group_id": self.group_id,
                "name": "Offline",
                "host": host,
                "force": False,
            },
        )
        self.assertEqual(status, 502)
        self.assertEqual(payload["data"]["code"], "PROBE_FAILED")
        self.assertEqual(len((await read_json(self.storage))["servers"]), 1)

        status, payload = await self._call(
            self.api.add_server,
            method="POST",
            body={
                "group_id": self.group_id,
                "name": "Offline",
                "host": host,
                "force": "true",
            },
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["data"]["code"], "INVALID_FORCE")

        status, payload = await self._call(
            self.api.add_server,
            method="POST",
            body={
                "group_id": self.group_id,
                "name": "Offline",
                "host": host,
                "force": None,
            },
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["data"]["code"], "INVALID_FORCE")

        status, payload = await self._call(
            self.api.add_server,
            method="POST",
            body={
                "group_id": self.group_id,
                "name": "Offline",
                "host": host,
                "force": True,
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(payload["data"]["server"]["name"], "Offline")
        self.assertEqual(payload["data"]["server"]["host"], host)
        self.assertEqual(self.probe_calls, [host])

        status, payload = await self._call(
            self.api.add_server,
            method="POST",
            body={
                "group_id": self.group_id,
                "name": "Another",
                "host": host,
                "force": True,
            },
        )
        self.assertEqual(status, 409)
        self.assertEqual(payload["data"]["code"], "DUPLICATE_HOST")

    async def test_update_only_name_and_host(self):
        data = await read_json(self.storage)
        data["servers"]["2"] = _server(2, "Beta", "beta.example:25565")
        await write_json(self.storage, data)

        status, payload = await self._call(
            self.api.update_server,
            method="POST",
            body={
                "group_id": self.group_id,
                "identifier": "1",
                "name": "Alpha New",
                "host": "alpha-new.example:25565",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["server"]["id"], 1)
        self.assertEqual(payload["data"]["server"]["name"], "Alpha New")
        self.assertEqual(payload["data"]["server"]["host"], "alpha-new.example:25565")
        self.assertIn("created_time", payload["data"]["server"])

        status, payload = await self._call(
            self.api.update_server,
            method="POST",
            body={
                "group_id": self.group_id,
                "identifier": "1",
                "failed_count": 99,
            },
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["data"]["code"], "UNSUPPORTED_FIELDS")

        status, payload = await self._call(
            self.api.update_server,
            method="POST",
            body={
                "group_id": self.group_id,
                "identifier": "1",
                "name": "Beta",
            },
        )
        self.assertEqual(status, 409)
        self.assertEqual(payload["data"]["code"], "DUPLICATE_NAME")

    async def test_delete_requires_confirm_and_cascades_trends(self):
        data = await read_json(self.storage)
        data["trends"] = {"1": {"history": [{"ts": self.now, "count": 3}]}}
        await write_json(self.storage, data)

        status, payload = await self._call(
            self.api.delete_server,
            method="POST",
            body={
                "group_id": self.group_id,
                "identifier": "Alpha",
                "confirm": False,
            },
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["data"]["code"], "CONFIRM_REQUIRED")

        status, payload = await self._call(
            self.api.delete_server,
            method="POST",
            body={
                "group_id": self.group_id,
                "identifier": "Alpha",
                "confirm": True,
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["data"]["deleted"])
        self.assertTrue(payload["data"]["trend_cascade_deleted"])
        self.assertTrue(payload["data"]["trend_existed"])
        persisted = await read_json(self.storage)
        self.assertNotIn("1", persisted["servers"])
        self.assertNotIn("1", persisted["trends"])

    async def test_status_success_failure_and_trend_write(self):
        await self._write_group(
            {
                "1": _server(
                    1,
                    "Alpha",
                    "alpha.example:25565",
                    failed_count=3,
                ),
                "2": _server(2, "Beta", "beta.example:25565"),
            }
        )
        self.statuses["alpha.example:25565"] = {
            "players_list": ["Alice", "Bob"],
            "latency": 42,
            "plays_max": 20,
            "plays_online": 2,
            "server_version": "1.21.4",
            "icon_base64": "aWNvbg==",
            "host": "alpha.example:25565",
        }
        self.statuses["beta.example:25565"] = None

        status, payload = await self._call(
            self.api.refresh_status,
            method="POST",
            body={"group_id": self.group_id},
        )
        self.assertEqual(status, 200)
        results = payload["data"]["servers"]
        self.assertEqual([item["state"] for item in results], ["online", "unreachable"])
        self.assertEqual(results[0]["players_sample"], ["Alice", "Bob"])
        self.assertFalse(results[0]["players_sample_complete"])
        self.assertEqual(results[0]["latency"], 42)
        self.assertEqual(results[0]["version"], "1.21.4")
        self.assertEqual(results[0]["players_online"], 2)
        self.assertEqual(results[0]["players_max"], 20)
        self.assertEqual(results[0]["icon_base64"], "aWNvbg==")
        self.assertIsNone(results[1]["players_online"])

        persisted = await read_json(self.storage)
        self.assertEqual(persisted["servers"]["1"]["failed_count"], 0)
        self.assertGreater(persisted["servers"]["1"]["last_success_time"], 0)
        self.assertEqual(persisted["servers"]["2"]["failed_count"], 1)
        self.assertGreater(persisted["servers"]["2"]["last_failed_time"], 0)
        self.assertEqual(
            persisted["trends"]["1"]["history"],
            [{"ts": self.now // 3600 * 3600, "count": 2}],
        )
        self.assertNotIn("2", persisted["trends"])

        self.probe_calls.clear()
        status, payload = await self._call(
            self.api.refresh_status,
            method="POST",
            body={"group_id": self.group_id, "identifier": "1"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["data"]["servers"]), 1)
        self.assertEqual(self.probe_calls, ["alpha.example:25565"])

    async def test_trend_hours_boundaries_and_per_server_statistics(self):
        bucket = self.now // 3600 * 3600
        await self._write_group(
            {
                "1": _server(1, "Alpha", "alpha.example:25565"),
                "2": _server(2, "Beta", "beta.example:25565"),
            },
            trends={
                "1": {
                    "history": [
                        {"ts": bucket - 3 * 3600, "count": 99},
                        {"ts": bucket - 2 * 3600, "count": 2},
                        {"ts": bucket, "count": 6},
                    ]
                },
                "2": {"history": [{"ts": bucket - 3600, "count": 5}]},
            },
        )

        status, payload = await self._call(
            self.api.get_trends,
            query={"group_id": self.group_id, "hours": "3"},
        )
        self.assertEqual(status, 200)
        results = payload["data"]["servers"]
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["points"], [
            {"ts": bucket - 2 * 3600, "count": 2},
            {"ts": bucket, "count": 6},
        ])
        self.assertEqual(results[0]["latest"], 6)
        self.assertEqual(results[0]["max"], 6)
        self.assertEqual(results[0]["average"], 4.0)
        self.assertEqual(results[0]["count"], 2)
        self.assertEqual(results[1]["count"], 1)
        self.assertNotIn({"ts": bucket - 3600, "count": 0}, results[0]["points"])

        status, payload = await self._call(
            self.api.get_trends,
            query={
                "group_id": self.group_id,
                "hours": "168",
                "identifier": "Beta",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["data"]["servers"]), 1)
        self.assertEqual(payload["data"]["servers"][0]["server"]["name"], "Beta")

        for invalid in ("0", "169", "abc"):
            status, payload = await self._call(
                self.api.get_trends,
                query={"group_id": self.group_id, "hours": invalid},
            )
            self.assertEqual(status, 400)
            self.assertEqual(payload["data"]["code"], "INVALID_HOURS")

        status, _ = await self._call(
            self.api.get_trends,
            query={"group_id": self.group_id, "hours": "1"},
        )
        self.assertEqual(status, 200)

    async def test_cleanup_preview_and_confirm(self):
        old = self.now - (AUTO_CLEANUP_DAYS + 1) * 24 * 3600
        recent = self.now - 3600
        await self._write_group(
            {
                "1": _server(
                    1,
                    "Old",
                    "old.example:25565",
                    last_success_time=old,
                    failed_count=8,
                ),
                "2": _server(
                    2,
                    "Recent",
                    "recent.example:25565",
                    last_success_time=recent,
                ),
            },
            trends={
                "1": {"history": [{"ts": old, "count": 1}]},
                "2": {"history": [{"ts": recent, "count": 3}]},
            },
        )

        status, payload = await self._call(
            self.api.preview_cleanup,
            query={"group_id": self.group_id},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["cleanup_days"], AUTO_CLEANUP_DAYS)
        self.assertEqual([item["id"] for item in payload["data"]["candidates"]], ["1"])

        status, payload = await self._call(
            self.api.execute_cleanup,
            method="POST",
            body={"group_id": self.group_id, "confirm": False},
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["data"]["code"], "CONFIRM_REQUIRED")

        status, payload = await self._call(
            self.api.execute_cleanup,
            method="POST",
            body={"group_id": self.group_id, "confirm": True},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["deleted_count"], 1)
        self.assertEqual(payload["data"]["deleted"][0]["id"], "1")
        persisted = await read_json(self.storage)
        self.assertNotIn("1", persisted["servers"])
        self.assertNotIn("1", persisted["trends"])
        self.assertIn("2", persisted["servers"])
