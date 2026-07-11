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

from data.plugins.astrbot_zhouyi_plugin.standalone_web import (
    PUBLIC_API_PREFIX,
    StandaloneWebService,
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
        response = await client.get(f"{PUBLIC_API_PREFIX}/bootstrap")
        self.assertEqual(response.status, 401)
        payload = await response.json()
        self.assertEqual(payload["data"]["code"], "AUTH_REQUIRED")

    async def test_cross_origin_post_is_forbidden(self):
        _, client = await self._start_service()
        response = await client.post(
            f"{PUBLIC_API_PREFIX}/status",
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
            f"{PUBLIC_API_PREFIX}/servers/add?group_id=123",
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
            "/api/v1/plugins/extensions/astrbot_zhouyi_plugin/page/servers/add",
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
            f"{PUBLIC_API_PREFIX}/bootstrap",
            headers={"Cookie": "astrbot_dashboard_jwt=expired"},
        )
        self.assertEqual(response.status, 401)
        payload = await response.json()
        self.assertEqual(payload["data"]["code"], "AUTH_REQUIRED")
        self.assertNotIn("detail", payload)

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
