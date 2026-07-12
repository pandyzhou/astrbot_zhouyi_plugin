from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PAGE_API_PATH = PLUGIN_ROOT / "livingmemory" / "core" / "page_api.py"
TEST_PACKAGE = "livingmemory_page_api_test_package"
DEFAULT_PLUGIN_NAME = "astrbot_zhouyi_plugin"
EXPECTED_ROUTES = {
    (f"/{DEFAULT_PLUGIN_NAME}/page/stats", ("GET",)),
    (f"/{DEFAULT_PLUGIN_NAME}/page/memories", ("GET",)),
    (f"/{DEFAULT_PLUGIN_NAME}/page/memories/detail", ("GET",)),
    (f"/{DEFAULT_PLUGIN_NAME}/page/memories/update", ("POST",)),
    (f"/{DEFAULT_PLUGIN_NAME}/page/memories/batch-delete", ("POST",)),
    (f"/{DEFAULT_PLUGIN_NAME}/page/memories/batch-update", ("POST",)),
    (f"/{DEFAULT_PLUGIN_NAME}/page/recall/test", ("POST",)),
    (f"/{DEFAULT_PLUGIN_NAME}/page/graph/overview", ("GET",)),
    (f"/{DEFAULT_PLUGIN_NAME}/page/graph/query", ("POST",)),
    (f"/{DEFAULT_PLUGIN_NAME}/page/backups", ("GET",)),
}
EXPECTED_PAGE_FILES = {
    "index.html",
    "styles.css",
    "app.js",
    "i18n.js",
    "graph-2d.js",
    "graph-ui.js",
    "modules/index.js",
    "modules/api-client.js",
    "modules/utils.js",
    "modules/memory-page.js",
    "modules/recall-page.js",
    "modules/system-page.js",
    "modules/peek-panel.js",
}


class _Context:
    def __init__(self) -> None:
        self.routes = []

    def register_web_api(self, path, handler, methods, description) -> None:
        self.routes.append((path, handler, tuple(methods), description))


class _Plugin:
    def __init__(self) -> None:
        self.context = _Context()
        self.initializer = None


def _load_page_api_module():
    package = types.ModuleType(TEST_PACKAGE)
    package.__path__ = []

    handlers = types.ModuleType(f"{TEST_PACKAGE}.page_api_modules")

    class _Handler:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

    for name in (
        "BackupHandler",
        "GraphHandler",
        "MemoryHandler",
        "PageApiUtils",
        "RecallHandler",
        "StatsHandler",
    ):
        setattr(handlers, name, _Handler)

    module_name = f"{TEST_PACKAGE}.page_api"
    spec = importlib.util.spec_from_file_location(module_name, PAGE_API_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载 LivingMemory Page API 模块")
    module = importlib.util.module_from_spec(spec)

    sys.modules[TEST_PACKAGE] = package
    sys.modules[handlers.__name__] = handlers
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class LivingMemoryPageApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.page_api_module = _load_page_api_module()

    def test_registers_ten_unique_routes_with_host_prefix(self) -> None:
        plugin = _Plugin()
        api = self.page_api_module.PluginPageApi(plugin)
        api.register_routes()

        registered = {(path, methods) for path, _, methods, _ in plugin.context.routes}
        paths = [path for path, _, _, _ in plugin.context.routes]

        self.assertEqual(len(plugin.context.routes), 10)
        self.assertEqual(len(paths), len(set(paths)))
        self.assertEqual(registered, EXPECTED_ROUTES)
        self.assertTrue(
            all(path.startswith(f"/{DEFAULT_PLUGIN_NAME}/page/") for path in paths)
        )
        self.assertTrue(all(not path.startswith("/mc") for path in paths))

    def test_constructor_supports_custom_plugin_name(self) -> None:
        plugin = _Plugin()
        api = self.page_api_module.PluginPageApi(plugin, plugin_name="custom_host")
        api.register_routes()

        self.assertEqual(api.plugin_name, "custom_host")
        self.assertEqual(api.page_api_prefix, "/custom_host/page")
        self.assertTrue(
            all(path.startswith("/custom_host/page/") for path, _, _, _ in plugin.context.routes)
        )

    def test_page_static_files_exist(self) -> None:
        page_root = PLUGIN_ROOT / "pages" / "livingmemory-dashboard"
        self.assertTrue(page_root.is_dir())
        missing = sorted(
            relative_path
            for relative_path in EXPECTED_PAGE_FILES
            if not (page_root / relative_path).is_file()
        )
        self.assertEqual(missing, [])

    def test_api_client_uses_bridge_page_paths_without_legacy_plugin_name(self) -> None:
        api_client = (
            PLUGIN_ROOT
            / "pages"
            / "livingmemory-dashboard"
            / "modules"
            / "api-client.js"
        ).read_text(encoding="utf-8")

        self.assertNotIn("astrbot_plugin_livingmemory", api_client)
        self.assertIn('return "page/"', api_client)
        self.assertIn("this.bridge.apiGet(this.buildEndpoint", api_client)
        self.assertIn("this.bridge.apiPost(this.buildEndpoint", api_client)


if __name__ == "__main__":
    unittest.main()
