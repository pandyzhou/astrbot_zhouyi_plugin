from __future__ import annotations

import re
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
        self.assertNotIn("setLocale", app)
        self.assertNotIn("zhouyi-dashboard-locale", app)
        self.assertNotIn("compact-control", app)
        self.assertNotIn("t('language')", app)
        for name in ("OverviewPage.tsx", "MemoriesPage.tsx", "RecallPage.tsx", "GraphPage.tsx"):
            self.assertTrue((APP_ROOT / "src" / "features" / "memory" / name).is_file())
        self.assertTrue((APP_ROOT / "src" / "features" / "settings" / "MemoryConfigPage.tsx").is_file())

    def test_page_headings_do_not_include_subtitles_after_h1(self):
        page_sources = [
            path
            for path in (APP_ROOT / "src").rglob("*.tsx")
            if "page-heading" in path.read_text(encoding="utf-8")
        ]
        self.assertTrue(page_sources)

        heading_pattern = re.compile(
            r'<header\b[^>]*className=["\']page-heading["\'][^>]*>(.*?)</header>',
            re.DOTALL,
        )
        subtitle_pattern = re.compile(r"</h1>\s*<p\b")
        for path in page_sources:
            source = path.read_text(encoding="utf-8")
            headings = heading_pattern.findall(source)
            with self.subTest(page=path.relative_to(APP_ROOT)):
                self.assertTrue(headings)
                for heading in headings:
                    self.assertIsNone(subtitle_pattern.search(heading))

    def test_page_data_cache_contracts(self):
        store = APP_ROOT / "src" / "store"
        core = (store / "queryCacheCore.ts").read_text(encoding="utf-8")
        hook = (store / "useCachedQuery.ts").read_text(encoding="utf-8")
        keys = (store / "queryKeys.ts").read_text(encoding="utf-8")
        self.assertIn("class QueryCacheCore", core)
        self.assertIn("inFlight", core)
        self.assertIn("invalidate(prefix", core)
        self.assertIn("generation", core)
        self.assertIn("useSyncExternalStore", hook)
        for key_factory in ("mcServers", "mcSettings", "mcTrends", "memoryList", "memoryGraphOverview"):
            self.assertIn(f"function {key_factory}", keys)
        self.assertIn("MEMORY_CONFIG_QUERY_PREFIX", keys)
        self.assertIn("memoryConfig", keys)
        self.assertIn("SOURCE_UPDATES_QUERY_PREFIX", keys)
        self.assertIn("sourceUpdates", keys)

        pages = {
            name: (APP_ROOT / "src" / relative).read_text(encoding="utf-8")
            for name, relative in {
                "servers": "features/servers/ServersPage.tsx",
                "trends": "features/trends/TrendsPage.tsx",
                "settings": "features/settings/SettingsPage.tsx",
                "memory_config": "features/settings/MemoryConfigPage.tsx",
                "overview": "features/memory/OverviewPage.tsx",
                "memories": "features/memory/MemoriesPage.tsx",
                "graph": "features/memory/GraphPage.tsx",
                "recall": "features/memory/RecallPage.tsx",
                "source_updates": "features/sources/SourceUpdatesPage.tsx",
            }.items()
        }
        for name in ("servers", "trends", "settings", "memory_config", "overview", "memories", "graph", "source_updates"):
            self.assertIn("useCachedQuery", pages[name], name)
        self.assertNotIn("useCachedQuery", pages["recall"])
        self.assertNotIn("setData(null)", pages["servers"])
        self.assertNotIn("setData(null)", pages["trends"])
        self.assertIn("if (!cachedData || dirty || saving || pendingPreview) return", pages["settings"])
        self.assertIn("MEMORY_LIST_QUERY_PREFIX", pages["memories"])
        self.assertIn("MEMORY_GRAPH_QUERY_PREFIX", pages["memories"])

    def test_trend_filters_are_kept_in_workshop_store(self):
        trends = (APP_ROOT / "src" / "features" / "trends" / "TrendsPage.tsx").read_text(encoding="utf-8")
        store = (APP_ROOT / "src" / "store" / "workshopStore.ts").read_text(encoding="utf-8")

        self.assertIn("trendFiltersByGroup: Record<string, TrendFiltersState>", store)
        self.assertIn("setTrendFilters: (groupId: string, filters: TrendFiltersState)", store)
        self.assertIn("state.trendFiltersByGroup[groupId]", trends)
        self.assertIn("setTrendFilters(groupId", trends)
        self.assertNotIn("useState<TrendFilters>", trends)

    def test_source_updates_page_contracts(self):
        source_page_path = APP_ROOT / "src" / "features" / "sources" / "SourceUpdatesPage.tsx"
        self.assertTrue(source_page_path.is_file())

        source_page = source_page_path.read_text(encoding="utf-8")
        app = (APP_ROOT / "src" / "App.tsx").read_text(encoding="utf-8")
        client = (APP_ROOT / "src" / "api" / "client.ts").read_text(encoding="utf-8")
        mock_client = (APP_ROOT / "src" / "api" / "mockClient.ts").read_text(encoding="utf-8")
        styles = (APP_ROOT / "src" / "styles.css").read_text(encoding="utf-8")
        all_frontend_source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (APP_ROOT / "src").rglob("*")
            if path.suffix in {".ts", ".tsx"}
        )

        self.assertIn('<NavLink to="/sources/updates">', app)
        self.assertNotIn('!standalone ? <NavLink to="/sources/updates">', app)
        self.assertIn('<Route path="/sources/updates" element={<SourceUpdatesPage />} />', app)
        self.assertNotIn('!standalone ? <Route path="/sources/updates"', app)
        self.assertIn('<NavLink to="/settings/memory">', app)
        self.assertIn('<Route path="/settings/memory" element={<MemoryConfigPage', app)
        self.assertNotIn('memoryAvailable ? <NavLink to="/settings/memory"', app)
        self.assertNotIn('standalone ? <Route path="/settings/memory"', app)
        self.assertIn("const standalone = !window.AstrBotPluginPage; const memoryAvailable = !standalone && Boolean(bootstrap?.capabilities.memory.available)", app)
        self.assertIn('{memoryAvailable ? <><NavLink to="/memory/overview">', app)
        self.assertIn('{memoryAvailable ? <><Route path="/memory/overview"', app)
        self.assertIn("const defaultPath = memoryAvailable ? '/memory/overview' : '/mc/servers';", app)

        self.assertIn("<h1>{t('sourceUpdates')}</h1>", source_page)
        self.assertIsNone(re.search(r"<h1>.*?</h1>\s*<p\b", source_page, re.DOTALL))
        self.assertNotIn("description=", source_page)

        self.assertIn("useCachedQuery<SourceUpdatesData>", source_page)
        self.assertIn("const SOURCE_UPDATES_TTL = 300_000", source_page)
        self.assertIn("{ ttl: SOURCE_UPDATES_TTL }", source_page)
        self.assertIn("sourceUpdatesQuery.setData(result)", source_page)
        self.assertNotIn("setData(null)", source_page)
        self.assertNotIn("setData(undefined)", source_page)
        self.assertIn("sourceUpdates: async (signal?: AbortSignal)", client)
        self.assertIn("refreshSourceUpdates: async (signal?: AbortSignal)", client)
        self.assertIn("normalizeSourceUpdates", client)
        self.assertIn("timestampOrNull", client)
        self.assertIn("raw.baseline_version", client)
        self.assertIn("raw.latest_commit", client)
        self.assertIn("'/page/v1/sources/updates'", client)
        self.assertIn("'/page/v1/sources/updates/refresh'", client)
        self.assertIn("method: 'POST'", client)
        self.assertIn("body: { force: true }", client)
        self.assertIn("/page/v1/bootstrap", mock_client)
        self.assertIn("/page/v1/sources/updates", mock_client)
        self.assertNotIn("api.github.com", all_frontend_source)

        for status in ("current", "new_version", "new_commits", "changed", "unavailable"):
            self.assertIn(f"{status}:", source_page)
        self.assertIn("source-status-chip", source_page)
        self.assertNotIn("StatusBadge", source_page)
        self.assertIn("safeExternalUrl", source_page)
        self.assertIn("url.protocol === 'https:'", source_page)
        self.assertIn('target="_blank"', source_page)
        self.assertIn('rel="noopener noreferrer"', source_page)

        self.assertIn(".source-refresh-button", styles)
        self.assertIn("min-height: 44px", styles)
        self.assertIn(".source-update-grid", styles)
        self.assertIn("grid-template-columns: repeat(2, minmax(0, 1fr))", styles)
        self.assertIn("@media (max-width: 900px)", styles)
        self.assertIn("grid-template-columns: 1fr", styles)

    def test_memory_config_page_contracts(self):
        settings_root = APP_ROOT / "src" / "features" / "settings"
        page = (settings_root / "MemoryConfigPage.tsx").read_text(encoding="utf-8")
        schema = (settings_root / "memoryConfigSchema.ts").read_text(encoding="utf-8")
        client = (APP_ROOT / "src" / "api" / "client.ts").read_text(encoding="utf-8")
        mock_client = (APP_ROOT / "src" / "api" / "mockClient.ts").read_text(encoding="utf-8")
        types = (APP_ROOT / "src" / "api" / "types.ts").read_text(encoding="utf-8")
        keys = (APP_ROOT / "src" / "store" / "queryKeys.ts").read_text(encoding="utf-8")
        schema_test = (settings_root / "memoryConfigSchema.test.ts").read_text(encoding="utf-8")
        app_package = (APP_ROOT / "package.json").read_text(encoding="utf-8")

        self.assertIn("parseMemoryConfigSchema", page)
        self.assertIn("createMemoryConfigDraft", page)
        self.assertIn("validateMemoryConfigDraft", page)
        self.assertIn("convertMemoryConfigDraft", page)
        self.assertIn("beforeunload", page)
        self.assertIn("expected_revision: data.revision", page)
        self.assertIn("queryCache.invalidate(queryKeyPrefixes.memory)", page)
        self.assertIn("config: result.config", page)
        self.assertIn("values: result.config", page)
        self.assertIn("runtime_id: result.runtime_id ?? data.runtime_id", page)
        self.assertIn("reload_status: result.reload_status ?? data.reload_status", page)
        self.assertIn("reload_failed: result.reload_failed ?? data.reload_failed", page)
        self.assertIn("result.old_runtime_id || data.runtime_id", page)
        self.assertIn("loaded.runtime_id !== oldRuntimeId", page)
        self.assertIn("revisionMatches(loaded.revision, expectedRevision)", page)
        self.assertIn("memoryConfigEquals(loaded.config, expectedConfig)", page)
        self.assertIn("loaded.reload_failed || loaded.reload_status === 'failed'", page)
        self.assertIn("配置已保存，但自动重载插件失败，请手动重载插件", page)
        self.assertIn("result.revision,\n        result.config,", page)
        self.assertIn("已被其他请求修改", page)
        self.assertIn(r"revision\s+conflict", page)
        self.assertIn("版本冲突", page)
        self.assertIn("重新加载并保留草稿", page)
        self.assertIn("if (!preserveDraft) setDraft", page)
        self.assertIn("applyLoadedData(loaded, preserveDraft)", page)

        for helper in (
            "deepClone",
            "getAtPath",
            "setAtPath",
            "countMemoryConfigChanges",
            "convertMemoryConfigDraft",
            "validateMemoryConfigDraft",
            "memoryConfigEquals",
        ):
            self.assertIn(f"function {helper}", schema)
        self.assertIn("Object.keys(left).sort()", schema)
        self.assertIn("memoryConfigEquals(value, right[index])", schema)
        self.assertIn("深比较忽略对象 key 顺序", schema_test)
        self.assertIn("AstrBot 默认", schema)
        self.assertIn("当前不可用", schema)
        self.assertIn("config: MemoryConfigObject", types)
        self.assertIn("config: raw.config as MemoryConfigData['config']", client)
        self.assertIn("reload_status: memoryReloadStatus(raw.reload_status)", client)
        self.assertIn("reload_failed: raw.reload_failed === true", client)
        self.assertIn("message: stringOrNull(raw.message) ?? undefined", client)
        self.assertIn("保存响应缺少规范化 config", client)
        self.assertIn("'/page/v1/config/memory'", client)
        self.assertIn("mutationKey: 'memory-config:save'", client)
        self.assertIn("/page/v1/config/memory", mock_client)
        self.assertIn("bot_language: 'zh'", mock_client)
        self.assertIn("provider_settings", mock_client)
        self.assertIn("recall_engine", mock_client)
        self.assertIn("config: clone(memoryConfigValues)", mock_client)
        self.assertNotIn("bot_language: 'zh-CN'", mock_client)
        self.assertNotIn("providers: { llm_provider_id", mock_client)
        self.assertNotIn("retrieval: {", mock_client)
        self.assertNotIn("storage: {", mock_client)
        self.assertIn('"test:memory-config"', app_package)
        self.assertIn("../../../temp/memory-config-tests", app_package)
        self.assertIn("MEMORY_CONFIG_QUERY_PREFIX = ['config', 'memory']", keys)

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
        self.assertIsNone(re.search(r"<WorkshopPanel\b[^>]*\bdescription=", settings, re.DOTALL))
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
        self.assertIn("disabled={saving || loading || !currentGroupLoaded}", settings)
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
        self.assertIn("const navigationLocked = settingsNavigationLocked || memoryConfigNavigationLocked", app)
        self.assertIn("disabled={loading || groups.length === 0 || navigationLocked}", app)
        self.assertIn("<SettingsPage onNavigationLockChange={setSettingsNavigationLocked} />", app)
        self.assertIn("<MemoryConfigPage onNavigationLockChange={setMemoryConfigNavigationLocked} />", app)
        self.assertIn("window.addEventListener('hashchange', handleHashChange)", app)
        self.assertIn("window.removeEventListener('hashchange', handleHashChange)", app)
        self.assertIn("approvedHashRef", app)
        self.assertIn("restoringHashRef", app)
        self.assertIn("window.location.hash = acceptedHashRef.current", app)
        self.assertIn("approved.expiresAt >= Date.now()", app)
        self.assertIn("event.preventDefault()", app)
        self.assertIn("onClickCapture={handleNavigationClick}", app)

        self.assertIn("const scopeRef = useRef<SettingsScope>('global')", settings)
        self.assertIn("useCachedQuery<SettingsData>", settings)
        self.assertIn("queryCache.revalidate(", settings)
        self.assertIn("applyLoadedData(loaded, nextScope)", settings)
        self.assertIn("scopeRef.current = nextScope", settings)

        self.assertNotIn("const controller = new AbortController()", settings)
        self.assertIn("groupIdRef.current !== requestedGroupId", settings)
        self.assertIn("value.group_id !== groupId", settings)
        self.assertIn("if (!cachedData || dirty || saving || pendingPreview) return", settings)
        self.assertIn("{loading ? <DataState state=\"loading\"", settings)
        self.assertIn("!loading && (blockingLoadError || !currentGroupLoaded)", settings)
        self.assertIn("state={blockingLoadError ? 'error' : 'empty'}", settings)
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
