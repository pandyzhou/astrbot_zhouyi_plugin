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

    def test_settings_navigation_source_contracts(self):
        settings = (APP_ROOT / "src" / "features" / "settings" / "SettingsPage.tsx").read_text(encoding="utf-8")
        for key in ("trend", "query", "cleanup", "experience"):
            self.assertIn(f"key: '{key}'", settings)
        self.assertIn("useState<SettingsSectionKey>('trend')", settings)
        self.assertIn("key={section.key}", settings)
        for class_name in (
            "settings-layout",
            "category-panel",
            "category-nav",
            "main-panel",
            "settings-fields",
            "summary-panel",
            "summary-list",
            "risk-copy",
            "summary-actions",
            "save-state",
        ):
            self.assertIn(class_name, settings)

    def test_settings_group_global_only_field_contracts(self):
        settings = (APP_ROOT / "src" / "features" / "settings" / "SettingsPage.tsx").read_text(encoding="utf-8")
        self.assertNotIn("if (scope === 'group' && key === 'max_concurrent_queries') return null", settings)
        self.assertIn("scope === 'group' && key === 'max_concurrent_queries'", settings)
        self.assertIn("disabled={saving || isInherited || globalOnly}", settings)
        self.assertIn("仅全局", settings)
        self.assertIn("data.global.max_concurrent_queries", settings)
        self.assertIn("（仅全局）", settings)
        self.assertIn("disabled={saving || loading || Boolean(loadError) || !currentGroupLoaded}", settings)
        group_keys = settings.split("const groupKeys: GroupRuntimeSettingKey[] = [", 1)[1].split("];", 1)[0]
        self.assertNotIn("'max_concurrent_queries'", group_keys)
        self.assertIn("const keys = scope === 'global' ? (Object.keys(data.global) as RuntimeSettingKey[]) : groupKeys", settings)
        self.assertIn("reset_keys: scope === 'group' ? [...inherited] : []", settings)

    def test_settings_scope_loading_and_navigation_lock_contracts(self):
        settings = (APP_ROOT / "src" / "features" / "settings" / "SettingsPage.tsx").read_text(encoding="utf-8")
        app = (APP_ROOT / "src" / "App.tsx").read_text(encoding="utf-8")

        self.assertIn("onNavigationLockChange?: (locked: boolean) => void", settings)
        self.assertIn("onNavigationLockChange?.(dirty || saving)", settings)
        self.assertIn("onNavigationLockChange?.(false)", settings)
        self.assertIn("useState(false)", app)
        self.assertIn("disabled={loading || groups.length === 0 || settingsNavigationLocked}", app)
        self.assertIn("<SettingsPage onNavigationLockChange={setSettingsNavigationLocked} />", app)

        self.assertIn("const scopeRef = useRef<SettingsScope>('global')", settings)
        self.assertIn("void load(scopeRef.current, controller.signal, groupId)", settings)
        self.assertIn("}, [groupId, load]);", settings)
        self.assertNotIn("[groupId, load, scope]", settings)
        self.assertIn("applyLoadedData(data, nextScope)", settings)
        self.assertIn("scopeRef.current = nextScope", settings)

        self.assertIn("const controller = new AbortController()", settings)
        self.assertIn("groupIdRef.current !== requestedGroupId", settings)
        self.assertIn("loaded.group_id !== requestedGroupId", settings)
        self.assertIn("{loading ? <DataState state=\"loading\"", settings)
        self.assertIn("!loading && (loadError || !currentGroupLoaded)", settings)
        self.assertIn("state={loadError ? 'error' : 'empty'}", settings)
        self.assertIn("onClick={() => void load(scopeRef.current)}", settings)
        self.assertIn(">重新加载</button>", settings)

    def test_settings_responsive_layout_and_unbounded_main_contracts(self):
        app_styles = (APP_ROOT / "src" / "styles.css").read_text(encoding="utf-8")
        ui_styles = (PLUGIN_ROOT / "web" / "packages" / "ui" / "src" / "styles.css").read_text(encoding="utf-8")
        self.assertIn("grid-template-columns: minmax(210px, 250px) minmax(0, 1fr) minmax(290px, 350px)", app_styles)
        self.assertIn(".category-nav", app_styles)
        self.assertIn(".summary-list", app_styles)
        self.assertIn(".summary-panel {\n    grid-column: 1 / -1;", app_styles)
        self.assertIn("overflow-x: auto", app_styles)
        self.assertIn("max-width: 320px", app_styles)
        self.assertIn("width: 100%;\n  max-width: none;\n  margin: 0;", ui_styles)
        self.assertIn("padding: 28px clamp(12px, 2vw, 36px) 48px", ui_styles)
        self.assertNotIn("width: min(1500px, 100%)", ui_styles)
        self.assertIn("position: sticky;\n    top: 0;\n    grid-template-columns: 1fr;", ui_styles)
        self.assertIn(".wf-topbar { position: sticky; top: 0; grid-template-columns: 1fr; }", app_styles)
        self.assertIn("overflow-x: clip", ui_styles)
        self.assertNotIn("overflow-x: hidden", ui_styles)
        self.assertNotIn(".wf-topbar { position: static", app_styles)


if __name__ == "__main__":
    unittest.main()
