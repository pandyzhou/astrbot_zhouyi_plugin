from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from yarl import URL

ASTRBOT_ROOT = Path(__file__).resolve().parents[4]
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(ASTRBOT_ROOT) not in sys.path:
    sys.path.insert(0, str(ASTRBOT_ROOT))

from data.plugins.astrbot_zhouyi_plugin import standalone_web as standalone_web_module
from data.plugins.astrbot_zhouyi_plugin.standalone_web import (
    PUBLIC_API_PREFIX,
    StandaloneWebService,
)


MEMORY_API_ROUTES = (
    ("GET", "/v1/memory/stats"),
    ("GET", "/v1/memory/backups"),
    ("GET", "/v1/memory/memories"),
    ("GET", "/v1/memory/memories/detail"),
    ("POST", "/v1/memory/memories/update"),
    ("POST", "/v1/memory/memories/batch-delete"),
    ("POST", "/v1/memory/memories/batch-update"),
    ("POST", "/v1/memory/recall/test"),
    ("GET", "/v1/memory/graph/overview"),
    ("POST", "/v1/memory/graph/query"),
)


class StandaloneWebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        temp_root = PLUGIN_ROOT / "temp"
        temp_root.mkdir(parents=True, exist_ok=True)
        self.temp_dir = tempfile.TemporaryDirectory(dir=temp_root)
        self.root = Path(self.temp_dir.name) / "page"
        self.assets = self.root / "assets"
        self.assets.mkdir(parents=True)
        (self.root / "index.html").write_text(
            "<!doctype html><title>standalone</title>", encoding="utf-8"
        )
        (self.assets / "index-AbC_123.js").write_text(
            "window.ready = true;", encoding="utf-8"
        )
        (self.assets / "plain.css").write_text("body{}", encoding="utf-8")
        self.clients: list[TestClient] = []
        self.servers: list[TestServer] = []

    async def asyncTearDown(self) -> None:
        for client in reversed(self.clients):
            await client.close()
        for server in reversed(self.servers):
            await server.close()
        self.temp_dir.cleanup()

    async def _start_service(
        self,
        *,
        upstream_base_url: str = "http://127.0.0.1:1",
        public_origin: str = "https://standalone.example:35020",
    ) -> tuple[StandaloneWebService, TestClient]:
        service = StandaloneWebService(
            page_root=self.root,
            upstream_base_url=upstream_base_url,
            public_origin=public_origin,
        )
        client = TestClient(TestServer(service.create_app()))
        await client.start_server()
        self.clients.append(client)
        return service, client

    async def _start_upstream(self, handler) -> TestServer:
        app = web.Application()
        app.router.add_route("*", "/{path:.*}", handler)
        server = TestServer(app)
        await server.start_server()
        self.servers.append(server)
        return server

    async def test_upstream_session_allows_long_batch_requests(self):
        service = StandaloneWebService(
            page_root=self.root,
            upstream_base_url="http://127.0.0.1:1",
        )
        app = service.create_app()
        client = TestClient(TestServer(app))
        await client.start_server()
        self.clients.append(client)

        timeout = app[standalone_web_module._SESSION_KEY].timeout
        self.assertEqual(timeout.total, 300)
        self.assertEqual(timeout.connect, 3)

    async def test_static_files_security_headers_and_cache_policy(self):
        _, client = await self._start_service()

        index = await client.get("/")
        self.assertEqual(index.status, 200)
        self.assertIn("standalone", await index.text())
        self.assertEqual(index.headers["Cache-Control"], "no-store")
        self.assertIn("frame-ancestors 'none'", index.headers["Content-Security-Policy"])
        self.assertEqual(index.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(index.headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(index.headers["X-Frame-Options"], "DENY")

        hashed = await client.get("/assets/index-AbC_123.js")
        self.assertEqual(hashed.status, 200)
        self.assertEqual(
            hashed.headers["Cache-Control"],
            "public, max-age=31536000, immutable",
        )
        self.assertEqual(await hashed.text(), "window.ready = true;")

        plain = await client.get("/assets/plain.css")
        self.assertEqual(plain.status, 200)
        self.assertEqual(plain.headers["Cache-Control"], "public, max-age=3600")

    async def test_static_rejects_traversal_symlink_escape_and_directories(self):
        outside = Path(self.temp_dir.name) / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        symlink = self.assets / "escape.txt"
        try:
            symlink.symlink_to(outside)
        except OSError:
            symlink = None

        _, client = await self._start_service()
        traversal_url = URL(
            f"{client.make_url('/')}assets/%2e%2e/outside.txt", encoded=True
        )
        traversal = await client.session.get(traversal_url)
        self.assertEqual(traversal.status, 404)

        directory = await client.get("/assets/")
        self.assertEqual(directory.status, 404)

        if symlink is not None:
            escaped = await client.get("/assets/escape.txt")
            self.assertEqual(escaped.status, 404)

    async def test_api_requires_dashboard_cookie(self):
        _, client = await self._start_service()
        response = await client.get(f"{PUBLIC_API_PREFIX}/v1/bootstrap")
        self.assertEqual(response.status, 401)
        payload = await response.json()
        self.assertEqual(payload["data"]["code"], "AUTH_REQUIRED")

    async def test_settings_routes_are_in_proxy_allowlist(self):
        seen: list[tuple[str, str]] = []

        async def upstream_handler(request: web.Request) -> web.Response:
            seen.append((request.method, request.path))
            return web.json_response({"ok": True})

        upstream = await self._start_upstream(upstream_handler)
        _, client = await self._start_service(upstream_base_url=str(upstream.make_url("/")))
        cookie = {"Cookie": "astrbot_dashboard_jwt=token"}
        post_headers = {
            **cookie,
            "Origin": "https://standalone.example:35020",
            "Sec-Fetch-Site": "same-origin",
        }

        get_response = await client.get(
            f"{PUBLIC_API_PREFIX}/v1/mc/settings?group_id=123",
            headers=cookie,
        )
        preview_response = await client.post(
            f"{PUBLIC_API_PREFIX}/v1/mc/settings/preview",
            json={},
            headers=post_headers,
        )
        save_response = await client.post(
            f"{PUBLIC_API_PREFIX}/v1/mc/settings",
            json={},
            headers=post_headers,
        )

        self.assertEqual(
            [get_response.status, preview_response.status, save_response.status],
            [200, 200, 200],
        )
        self.assertEqual(
            seen,
            [
                (
                    "GET",
                    "/api/v1/plugins/extensions/astrbot_zhouyi_plugin/page/v1/mc/settings",
                ),
                (
                    "POST",
                    "/api/v1/plugins/extensions/astrbot_zhouyi_plugin/page/v1/mc/settings/preview",
                ),
                (
                    "POST",
                    "/api/v1/plugins/extensions/astrbot_zhouyi_plugin/page/v1/mc/settings",
                ),
            ],
        )

    async def test_memory_config_routes_are_explicitly_allowlisted(self):
        seen: list[tuple[str, str, object | None]] = []

        async def upstream_handler(request: web.Request) -> web.Response:
            body = await request.json() if request.method == "POST" else None
            seen.append((request.method, request.path, body))
            return web.json_response({"ok": True}, status=202 if body else 200)

        upstream = await self._start_upstream(upstream_handler)
        _, client = await self._start_service(upstream_base_url=str(upstream.make_url("/")))
        cookie = {"Cookie": "astrbot_dashboard_jwt=token"}
        post_headers = {
            **cookie,
            "Origin": "https://standalone.example:35020",
            "Sec-Fetch-Site": "same-origin",
        }

        get_response = await client.get(
            f"{PUBLIC_API_PREFIX}/v1/config/memory",
            headers=cookie,
        )
        post_response = await client.post(
            f"{PUBLIC_API_PREFIX}/v1/config/memory",
            json={"config": {}, "expected_revision": "revision"},
            headers=post_headers,
        )

        self.assertEqual([get_response.status, post_response.status], [200, 202])
        self.assertEqual(
            seen,
            [
                (
                    "GET",
                    "/api/v1/plugins/extensions/astrbot_zhouyi_plugin/page/v1/config/memory",
                    None,
                ),
                (
                    "POST",
                    "/api/v1/plugins/extensions/astrbot_zhouyi_plugin/page/v1/config/memory",
                    {"config": {}, "expected_revision": "revision"},
                ),
            ],
        )

    async def test_memory_routes_proxy_exact_paths_queries_and_bodies(self):
        seen: list[tuple[str, str, str, bytes]] = []

        async def upstream_handler(request: web.Request) -> web.Response:
            seen.append(
                (
                    request.method,
                    request.path,
                    request.query_string,
                    await request.read(),
                )
            )
            return web.json_response({"ok": True})

        upstream = await self._start_upstream(upstream_handler)
        _, client = await self._start_service(upstream_base_url=str(upstream.make_url("/")))
        cookie = {"Cookie": "astrbot_dashboard_jwt=token"}
        post_headers = {
            **cookie,
            "Origin": "https://standalone.example:35020",
            "Sec-Fetch-Site": "same-origin",
            "Content-Type": "application/json",
        }

        expected: list[tuple[str, str, str, bytes]] = []
        for index, (method, suffix) in enumerate(MEMORY_API_ROUTES):
            upstream_path = (
                "/api/v1/plugins/extensions/astrbot_zhouyi_plugin/page" + suffix
            )
            if method == "GET":
                query = f"marker={index}&scope=all"
                response = await client.get(
                    f"{PUBLIC_API_PREFIX}{suffix}?{query}",
                    headers=cookie,
                )
                expected.append((method, upstream_path, query, b""))
            else:
                body = json.dumps(
                    {"route": suffix, "index": index},
                    separators=(",", ":"),
                ).encode()
                response = await client.post(
                    f"{PUBLIC_API_PREFIX}{suffix}",
                    data=body,
                    headers=post_headers,
                )
                expected.append((method, upstream_path, "", body))
            self.assertEqual(response.status, 200, suffix)

        self.assertEqual(seen, expected)

    async def test_memory_post_preserves_security_boundaries(self):
        upstream_calls = 0

        async def upstream_handler(request: web.Request) -> web.Response:
            nonlocal upstream_calls
            upstream_calls += 1
            return web.json_response({"ok": True})

        upstream = await self._start_upstream(upstream_handler)
        _, client = await self._start_service(upstream_base_url=str(upstream.make_url("/")))
        path = f"{PUBLIC_API_PREFIX}/v1/memory/recall/test"
        cookie = {"Cookie": "astrbot_dashboard_jwt=token"}
        valid_origin = "https://standalone.example:35020"
        valid_headers = {
            **cookie,
            "Origin": valid_origin,
            "Sec-Fetch-Site": "same-origin",
        }

        valid = await client.post(path, json={"query": "hello"}, headers=valid_headers)
        missing_cookie = await client.post(
            path,
            json={},
            headers={"Origin": valid_origin, "Sec-Fetch-Site": "same-origin"},
        )
        missing_origin = await client.post(
            path,
            json={},
            headers={**cookie, "Sec-Fetch-Site": "same-origin"},
        )
        wrong_origin = await client.post(
            path,
            json={},
            headers={
                **cookie,
                "Origin": "https://evil.example",
                "Sec-Fetch-Site": "same-origin",
            },
        )
        cross_site = await client.post(
            path,
            json={},
            headers={
                **cookie,
                "Origin": valid_origin,
                "Sec-Fetch-Site": "cross-site",
            },
        )
        non_json = await client.post(
            path,
            data="{}",
            headers={
                **valid_headers,
                "Content-Type": "text/plain",
            },
        )
        oversized = await client.post(
            path,
            data=b"x" * (standalone_web_module.MAX_REQUEST_BODY + 1),
            headers={
                **valid_headers,
                "Content-Type": "application/json",
            },
        )

        rejected = [
            missing_cookie,
            missing_origin,
            wrong_origin,
            cross_site,
            non_json,
            oversized,
        ]
        self.assertEqual(valid.status, 200)
        self.assertEqual(
            [response.status for response in rejected],
            [401, 403, 403, 403, 415, 413],
        )
        self.assertEqual(
            [(await response.json())["data"]["code"] for response in rejected],
            [
                "AUTH_REQUIRED",
                "ORIGIN_FORBIDDEN",
                "ORIGIN_FORBIDDEN",
                "FETCH_SITE_FORBIDDEN",
                "INVALID_CONTENT_TYPE",
                "REQUEST_TOO_LARGE",
            ],
        )
        self.assertEqual(upstream_calls, 1)

    async def test_cross_origin_post_is_forbidden(self):
        _, client = await self._start_service()
        response = await client.post(
            f"{PUBLIC_API_PREFIX}/v1/mc/status",
            json={"group_id": "123"},
            headers={
                "Origin": "https://evil.example",
                "Sec-Fetch-Site": "cross-site",
                "Cookie": "astrbot_dashboard_jwt=token",
            },
        )
        self.assertEqual(response.status, 403)
        payload = await response.json()
        self.assertEqual(payload["data"]["code"], "ORIGIN_FORBIDDEN")

    async def test_valid_proxy_rewrites_path_and_forwards_only_allowed_headers(self):
        seen: dict[str, object] = {}

        async def upstream_handler(request: web.Request) -> web.Response:
            seen["method"] = request.method
            seen["path"] = request.path
            seen["query"] = request.query_string
            seen["headers"] = dict(request.headers)
            seen["body"] = await request.json()
            return web.json_response(
                {"proxied": True, "group_id": request.query.get("group_id")},
                status=201,
            )

        upstream = await self._start_upstream(upstream_handler)
        _, client = await self._start_service(upstream_base_url=str(upstream.make_url("/")))
        response = await client.post(
            f"{PUBLIC_API_PREFIX}/v1/mc/servers/add?group_id=123",
            json={"name": "Alpha"},
            headers={
                "Origin": "https://standalone.example:35020",
                "Sec-Fetch-Site": "same-origin",
                "Cookie": "astrbot_dashboard_jwt=token; theme=dark",
                "Accept": "application/json",
                "Authorization": "Bearer must-not-forward",
                "X-API-Key": "must-not-forward",
            },
        )

        self.assertEqual(response.status, 201)
        self.assertEqual(
            await response.json(), {"proxied": True, "group_id": "123"}
        )
        self.assertEqual(seen["method"], "POST")
        self.assertEqual(
            seen["path"],
            "/api/v1/plugins/extensions/astrbot_zhouyi_plugin/page/v1/mc/servers/add",
        )
        self.assertEqual(seen["query"], "group_id=123")
        self.assertEqual(seen["body"], {"name": "Alpha"})
        forwarded_headers = seen["headers"]
        self.assertEqual(
            forwarded_headers["Cookie"],
            "astrbot_dashboard_jwt=token; theme=dark",
        )
        self.assertEqual(forwarded_headers["Accept"], "application/json")
        self.assertTrue(
            forwarded_headers["Content-Type"].startswith("application/json")
        )
        self.assertNotIn("Authorization", forwarded_headers)
        self.assertNotIn("X-API-Key", forwarded_headers)

    async def test_upstream_401_is_normalized_to_auth_required(self):
        async def upstream_handler(request: web.Request) -> web.Response:
            return web.json_response({"detail": "expired"}, status=401)

        upstream = await self._start_upstream(upstream_handler)
        _, client = await self._start_service(upstream_base_url=str(upstream.make_url("/")))
        response = await client.get(
            f"{PUBLIC_API_PREFIX}/v1/bootstrap",
            headers={"Cookie": "astrbot_dashboard_jwt=expired"},
        )
        self.assertEqual(response.status, 401)
        payload = await response.json()
        self.assertEqual(payload["data"]["code"], "AUTH_REQUIRED")
        self.assertNotIn("detail", payload)

    async def test_source_update_routes_proxy_to_upstream(self):
        seen: list[tuple[str, str, str, object | None]] = []

        async def upstream_handler(request: web.Request) -> web.Response:
            body = await request.json() if request.method == "POST" else None
            seen.append((request.method, request.path, request.query_string, body))
            return web.json_response({"ok": True})

        upstream = await self._start_upstream(upstream_handler)
        _, client = await self._start_service(upstream_base_url=str(upstream.make_url("/")))
        cookie = {"Cookie": "astrbot_dashboard_jwt=token"}
        post_headers = {
            **cookie,
            "Origin": "https://standalone.example:35020",
            "Sec-Fetch-Site": "same-origin",
        }

        get_response = await client.get(
            f"{PUBLIC_API_PREFIX}/v1/sources/updates?channel=stable",
            headers=cookie,
        )
        refresh_response = await client.post(
            f"{PUBLIC_API_PREFIX}/v1/sources/updates/refresh",
            json={"force": True},
            headers=post_headers,
        )

        self.assertEqual([get_response.status, refresh_response.status], [200, 200])
        self.assertEqual(
            seen,
            [
                (
                    "GET",
                    "/api/v1/plugins/extensions/astrbot_zhouyi_plugin/page/v1/sources/updates",
                    "channel=stable",
                    None,
                ),
                (
                    "POST",
                    "/api/v1/plugins/extensions/astrbot_zhouyi_plugin/page/v1/sources/updates/refresh",
                    "",
                    {"force": True},
                ),
            ],
        )

    async def test_source_refresh_preserves_post_security_boundaries(self):
        upstream_calls = 0

        async def upstream_handler(request: web.Request) -> web.Response:
            nonlocal upstream_calls
            upstream_calls += 1
            return web.json_response({"ok": True})

        upstream = await self._start_upstream(upstream_handler)
        _, client = await self._start_service(upstream_base_url=str(upstream.make_url("/")))
        refresh_path = f"{PUBLIC_API_PREFIX}/v1/sources/updates/refresh"
        cookie = {"Cookie": "astrbot_dashboard_jwt=token"}
        valid_origin = "https://standalone.example:35020"

        missing_cookie = await client.post(
            refresh_path,
            json={},
            headers={"Origin": valid_origin, "Sec-Fetch-Site": "same-origin"},
        )
        missing_origin = await client.post(
            refresh_path,
            json={},
            headers={**cookie, "Sec-Fetch-Site": "same-origin"},
        )
        wrong_origin = await client.post(
            refresh_path,
            json={},
            headers={
                **cookie,
                "Origin": "https://evil.example",
                "Sec-Fetch-Site": "same-origin",
            },
        )
        cross_site = await client.post(
            refresh_path,
            json={},
            headers={
                **cookie,
                "Origin": valid_origin,
                "Sec-Fetch-Site": "cross-site",
            },
        )
        non_json = await client.post(
            refresh_path,
            data="{}",
            headers={
                **cookie,
                "Origin": valid_origin,
                "Sec-Fetch-Site": "same-origin",
                "Content-Type": "text/plain",
            },
        )
        oversized = await client.post(
            refresh_path,
            data=b"x" * (standalone_web_module.MAX_REQUEST_BODY + 1),
            headers={
                **cookie,
                "Origin": valid_origin,
                "Sec-Fetch-Site": "same-origin",
                "Content-Type": "application/json",
            },
        )

        responses = [
            missing_cookie,
            missing_origin,
            wrong_origin,
            cross_site,
            non_json,
            oversized,
        ]
        self.assertEqual(
            [response.status for response in responses],
            [401, 403, 403, 403, 415, 413],
        )
        self.assertEqual(
            [(await response.json())["data"]["code"] for response in responses],
            [
                "AUTH_REQUIRED",
                "ORIGIN_FORBIDDEN",
                "ORIGIN_FORBIDDEN",
                "FETCH_SITE_FORBIDDEN",
                "INVALID_CONTENT_TYPE",
                "REQUEST_TOO_LARGE",
            ],
        )
        self.assertEqual(upstream_calls, 0)

    async def test_memory_unlisted_suffixes_wrong_methods_and_legacy_paths_stay_closed(self):
        _, client = await self._start_service()
        cookie = {"Cookie": "astrbot_dashboard_jwt=token"}
        post_headers = {
            **cookie,
            "Origin": "https://standalone.example:35020",
            "Sec-Fetch-Site": "same-origin",
        }

        async def request(method: str, suffix: str):
            if method == "POST":
                return await client.post(
                    f"{PUBLIC_API_PREFIX}{suffix}",
                    json={},
                    headers=post_headers,
                )
            return await client.get(
                f"{PUBLIC_API_PREFIX}{suffix}",
                headers=cookie,
            )

        unlisted = [
            await request("GET", "/v1/memory"),
            await request("GET", "/v1/memory/unknown"),
            await request("GET", "/v1/memory/stats/extra"),
            await request("POST", "/v1/memory/graph/query/extra"),
        ]
        self.assertEqual([response.status for response in unlisted], [404] * 4)

        wrong_methods = []
        legacy_paths = []
        for method, suffix in MEMORY_API_ROUTES:
            wrong_method = "POST" if method == "GET" else "GET"
            wrong_methods.append(await request(wrong_method, suffix))
            legacy_suffix = suffix.removeprefix("/v1/memory")
            legacy_paths.append(await request(method, legacy_suffix))

        self.assertEqual(
            [response.status for response in wrong_methods],
            [405] * len(MEMORY_API_ROUTES),
        )
        self.assertEqual(
            [response.status for response in legacy_paths],
            [404] * len(MEMORY_API_ROUTES),
        )

    async def test_other_unlisted_routes_and_wrong_methods_remain_closed(self):
        _, client = await self._start_service()
        cookie = {"Cookie": "astrbot_dashboard_jwt=token"}
        post_headers = {
            **cookie,
            "Origin": "https://standalone.example:35020",
            "Sec-Fetch-Site": "same-origin",
        }

        config_root_response = await client.get(
            f"{PUBLIC_API_PREFIX}/v1/config",
            headers=cookie,
        )
        config_other_response = await client.get(
            f"{PUBLIC_API_PREFIX}/v1/config/memory/extra",
            headers=cookie,
        )
        source_root_response = await client.get(
            f"{PUBLIC_API_PREFIX}/v1/sources",
            headers=cookie,
        )
        source_other_response = await client.get(
            f"{PUBLIC_API_PREFIX}/v1/sources/updates/history",
            headers=cookie,
        )
        wrong_get_response = await client.get(
            f"{PUBLIC_API_PREFIX}/v1/sources/updates/refresh",
            headers=cookie,
        )
        wrong_post_response = await client.post(
            f"{PUBLIC_API_PREFIX}/v1/sources/updates",
            json={},
            headers=post_headers,
        )

        self.assertEqual(config_root_response.status, 404)
        self.assertEqual(config_other_response.status, 404)
        self.assertEqual(source_root_response.status, 404)
        self.assertEqual(source_other_response.status, 404)
        self.assertEqual(wrong_get_response.status, 405)
        self.assertEqual(wrong_post_response.status, 405)

    async def test_run_and_stop_are_idempotent(self):
        service = StandaloneWebService(
            host="127.0.0.1",
            port=0,
            page_root=self.root,
            upstream_base_url="http://127.0.0.1:1",
            ssl_context=None,
        )
        first_run = asyncio.create_task(service.run())
        second_run = asyncio.create_task(service.run())
        for _ in range(100):
            if service._runner is not None:
                break
            await asyncio.sleep(0)
        self.assertIsNotNone(service._runner)

        await asyncio.gather(service.stop(), service.stop())
        await asyncio.gather(first_run, second_run)
        self.assertIsNone(service._runner)
        self.assertIsNone(service._site)

    async def test_bom_config_supplies_dashboard_port(self):
        config_path = Path(self.temp_dir.name) / "cmd_config.json"
        config_path.write_bytes(
            ("\ufeff" + json.dumps({
                "dashboard": {
                    "port": 45678,
                    "ssl": {
                        "enable": True,
                        "cert_file": "cert.pem",
                        "key_file": "key.pem",
                    },
                }
            })).encode("utf-8")
        )
        service = StandaloneWebService(page_root=self.root, config_path=config_path)
        self.assertEqual(service._upstream_base_url(), "https://127.0.0.1:45678")


if __name__ == "__main__":
    unittest.main()
