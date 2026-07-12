from __future__ import annotations

import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = PLUGIN_ROOT / "web" / "packages" / "app"
PAGE_ROOT = PLUGIN_ROOT / "pages" / "zhouyi-dashboard"


class ZhouyiDashboardPageTests(unittest.TestCase):
    def test_only_one_business_page_directory_remains(self):
        self.assertTrue(PAGE_ROOT.is_dir())
        self.assertFalse((PLUGIN_ROOT / "pages" / "mc-manager").exists())
        self.assertFalse((PLUGIN_ROOT / "pages" / "livingmemory-dashboard").exists())

    def test_static_build_contains_index_and_hashed_assets(self):
        self.assertTrue((PAGE_ROOT / "index.html").is_file())
        assets = PAGE_ROOT / "assets"
        self.assertTrue(assets.is_dir())
        self.assertTrue(any(path.suffix == ".js" for path in assets.iterdir()))
        self.assertTrue(any(path.suffix == ".css" for path in assets.iterdir()))
        index = (PAGE_ROOT / "index.html").read_text(encoding="utf-8")
        self.assertIn("Zhouyi Dashboard", index)

    def test_react_source_covers_memory_features_and_lazy_graph(self):
        app = (APP_ROOT / "src" / "App.tsx").read_text(encoding="utf-8")
        self.assertIn("lazy(() => import('./features/memory/GraphPage'))", app)
        self.assertIn("/memory/overview", app)
        self.assertIn("/memory/memories", app)
        self.assertIn("/memory/recall", app)
        self.assertIn("/memory/graph", app)
        self.assertIn("standalone", app)
        for name in ("OverviewPage.tsx", "MemoriesPage.tsx", "RecallPage.tsx", "GraphPage.tsx"):
            self.assertTrue((APP_ROOT / "src" / "features" / "memory" / name).is_file())

    def test_api_client_uses_v1_namespaces(self):
        client = (APP_ROOT / "src" / "api" / "client.ts").read_text(encoding="utf-8")
        self.assertIn("/page/v1/bootstrap", client)
        self.assertIn("/page/v1/mc/servers", client)
        self.assertIn("/page/v1/memory/", client)
        self.assertNotIn("astrbot_plugin_livingmemory", client)

    def test_i18n_theme_and_accessibility_rules_exist(self):
        i18n = (APP_ROOT / "src" / "i18n.ts").read_text(encoding="utf-8")
        styles = (APP_ROOT / "src" / "styles.css").read_text(encoding="utf-8")
        self.assertIn("zh:", i18n)
        self.assertIn("en:", i18n)
        self.assertIn("ru:", i18n)
        self.assertIn(':root[data-theme="light"]', styles)
        self.assertIn("prefers-reduced-motion", styles)
        self.assertIn("min-height: 44px", styles)


if __name__ == "__main__":
    unittest.main()
