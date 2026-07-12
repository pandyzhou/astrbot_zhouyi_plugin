from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ASTRBOT_ROOT = Path(__file__).resolve().parents[4]
if str(ASTRBOT_ROOT) not in sys.path:
    sys.path.insert(0, str(ASTRBOT_ROOT))

from data.plugins.astrbot_zhouyi_plugin.web_api import PAGE_API_PREFIX
from data.plugins.astrbot_zhouyi_plugin.zhouyi_page_api import (
    MC_V1_PREFIX,
    MEMORY_ROUTE_DESCRIPTORS,
    MEMORY_V1_PREFIX,
    PAGE_V1_PREFIX,
    ZhouyiDashboardApi,
)


class _Context:
    def __init__(self) -> None:
        self.routes = []

    def register_web_api(self, path, handler, methods, description) -> None:
        key = (path, tuple(methods))
        self.routes = [route for route in self.routes if (route[0], tuple(route[2])) != key]
        self.routes.append((path, handler, methods, description))


class _Plugin:
    def __init__(self, runtime=None) -> None:
        self.context = _Context()
        self.runtime = runtime


class _MemoryService:
    enabled = True
    initialized = True

    async def get_stats(self):
        return {"status": "ok", "data": {"total_memories": 3}}


class ZhouyiDashboardApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_registers_new_routes_and_all_legacy_aliases_once(self):
        plugin = _Plugin()
        api = ZhouyiDashboardApi(plugin, _MemoryService())
        api.register_routes()
        api.register_routes()
        registered = {(path, tuple(methods)) for path, _, methods, _ in plugin.context.routes}
        self.assertEqual(len(plugin.context.routes), 45)
        self.assertEqual(len(registered), 45)
        self.assertIn((f"{PAGE_V1_PREFIX}/bootstrap", ("GET",)), registered)
        self.assertIn((f"{PAGE_API_PREFIX}/bootstrap", ("GET",)), registered)
        self.assertIn((f"{MC_V1_PREFIX}/bootstrap", ("GET",)), registered)
        self.assertIn((f"{MC_V1_PREFIX}/servers", ("GET",)), registered)
        self.assertIn((f"{MC_V1_PREFIX}/settings", ("POST",)), registered)
        self.assertIn((f"{MEMORY_V1_PREFIX}/memories/detail", ("GET",)), registered)
        for suffix, _, methods, _ in MEMORY_ROUTE_DESCRIPTORS:
            self.assertIn((f"{MEMORY_V1_PREFIX}{suffix}", methods), registered)
            self.assertIn((f"{PAGE_API_PREFIX}{suffix}", methods), registered)

    async def test_bootstrap_keeps_mc_available_when_memory_is_disabled(self):
        plugin = _Plugin()
        api = ZhouyiDashboardApi(plugin, None)

        async def group_storages():
            return {"200": object(), "100": object()}

        api.mc_api._group_storages = group_storages
        response = await api.bootstrap()
        payload = json.loads(response.body.decode("utf-8"))
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["data"]["brand"], "Zhouyi Dashboard")
        self.assertEqual(payload["data"]["groups"], [{"id": "100"}, {"id": "200"}])
        self.assertTrue(payload["data"]["capabilities"]["mc"]["available"])
        self.assertFalse(payload["data"]["capabilities"]["memory"]["available"])
        self.assertEqual(payload["data"]["capabilities"]["memory"]["reason"], "not_enabled")

    async def test_enabled_memory_startup_failure_uses_runtime_capability(self):
        runtime = type(
            "Runtime",
            (),
            {
                "memory_enabled": True,
                "memory_error": "memory migration failed",
            },
        )()
        api = ZhouyiDashboardApi(_Plugin(runtime), None)

        async def group_storages():
            return {"100": object()}

        api.mc_api._group_storages = group_storages
        payload = json.loads((await api.bootstrap()).body.decode("utf-8"))["data"]
        capability = payload["capabilities"]["memory"]
        self.assertTrue(capability["enabled"])
        self.assertFalse(capability["available"])
        self.assertFalse(capability["initialized"])
        self.assertEqual(capability["error"], "memory migration failed")
        self.assertEqual(capability["reason"], "unavailable")

    async def test_memory_status_failure_is_reported_in_bootstrap(self):
        class BrokenMemory:
            enabled = True
            initialized = False

            async def get_capability_status(self):
                raise RuntimeError("memory init failed")

        api = ZhouyiDashboardApi(_Plugin(), BrokenMemory())

        async def group_storages():
            return {"100": object()}

        api.mc_api._group_storages = group_storages
        payload = json.loads((await api.bootstrap()).body.decode("utf-8"))["data"]
        self.assertEqual(payload["groups"], [{"id": "100"}])
        self.assertFalse(payload["capabilities"]["memory"]["available"])
        self.assertIn("memory init failed", payload["capabilities"]["memory"]["error"])


if __name__ == "__main__":
    unittest.main()
