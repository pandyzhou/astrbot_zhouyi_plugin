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

    def test_memory_detail_drawer_reading_workspace_contracts(self):
        drawer = (APP_ROOT / "src" / "features" / "memory" / "MemoryDetailDrawer.tsx").read_text(encoding="utf-8")
        styles = (APP_ROOT / "src" / "styles.css").read_text(encoding="utf-8")

        for class_name in (
            "memory-detail-layout",
            "memory-detail-main",
            "memory-detail-sidebar",
            "memory-drawer-body",
            "memory-drawer-actions",
        ):
            self.assertIn(class_name, drawer)
            self.assertIn(f".{class_name}", styles)

        self.assertIn('<article className="memory-detail-main">', drawer)
        self.assertIn('<aside className="memory-detail-sidebar"', drawer)
        self.assertIn('<details className="memory-detail-disclosure">', drawer)
        self.assertIn("<summary>{t('updateHistory')}</summary>", drawer)
        self.assertIn("<summary>{t('graphContext')}</summary>", drawer)
        self.assertIn("createPortal(", drawer)
        self.assertIn("document.body,", drawer)
        self.assertIn("document.body.style.overflow = 'hidden'", drawer)
        self.assertIn("document.body.style.overflow = previousBodyOverflow", drawer)
        self.assertIn('role="dialog"', drawer)
        self.assertIn('aria-modal="true"', drawer)
        self.assertIn('aria-labelledby="memory-detail-title"', drawer)
        self.assertIn('aria-busy={busy}', drawer)

        self.assertIn("width: min(1120px, calc(100vw - 48px))", styles)
        self.assertIn("height: calc(100dvh - 48px)", styles)
        self.assertRegex(
            styles,
            re.compile(
                r"\.memory-detail-content \{.*?max-width: 72ch;.*?font-size: 16px;.*?line-height: 1\.75;"
                r".*?white-space: pre-wrap;.*?overflow-wrap: break-word;",
                re.DOTALL,
            ),
        )
        self.assertRegex(
            styles,
            re.compile(
                r"@media \(max-width: 900px\) \{.*?\.memory-detail-layout \{ grid-template-columns: 1fr; \}",
                re.DOTALL,
            ),
        )
        self.assertRegex(
            styles,
            re.compile(
                r"@media \(max-width: 560px\) \{.*?\.memory-drawer \{ width: 100vw; height: 100dvh;"
                r".*?border: 0;.*?\.memory-drawer \.wf-button, \.memory-detail-disclosure summary"
                r" \{ min-width: 44px; min-height: 44px; \}",
                re.DOTALL,
            ),
        )

    def test_recall_page_workbench_contracts(self):
        recall = (APP_ROOT / "src" / "features" / "memory" / "RecallPage.tsx").read_text(encoding="utf-8")
        recall_sessions = (APP_ROOT / "src" / "features" / "memory" / "recallSessions.ts").read_text(encoding="utf-8")
        memory_types = (APP_ROOT / "src" / "features" / "memory" / "types.ts").read_text(encoding="utf-8")
        styles = (APP_ROOT / "src" / "styles.css").read_text(encoding="utf-8")

        for class_name in (
            "recall-workspace",
            "recall-options",
            "recall-run-button",
            "recall-result-summary",
            "recall-result-card",
            "recall-result-layout",
            "recall-result-score",
            "recall-score-disclosure",
        ):
            self.assertIn(class_name, recall)
            self.assertIn(f".{class_name}", styles)

        self.assertIn("<WorkshopPanel title={t('hybridRetrieval')}>", recall)
        self.assertIn("<WorkshopPanel title={t('results')}>", recall)
        self.assertNotIn("description=", recall)
        self.assertRegex(
            recall,
            re.compile(r"import \{[^}]*\bSelectField\b[^}]*\} from '@pandyzhou/astrbot-mc-ui';"),
        )
        self.assertIn("useCachedQuery<StatsData>", recall)
        self.assertIn("queryKeys.memoryOverviewStats", recall)
        self.assertIn("() => memoryGet<StatsData>('stats')", recall)
        self.assertIn("export interface RecallSession", memory_types)
        self.assertIn("recall_sessions?: RecallSession[]", memory_types)
        self.assertIn("sessions?: Record<string, number>", memory_types)
        self.assertIn("recent_sessions?: Array<", memory_types)
        self.assertIn("statsQuery.data?.recall_sessions", recall)
        self.assertNotIn("statsQuery.data?.sessions", recall)
        self.assertNotIn("statsQuery.data.sessions", recall)
        self.assertNotIn("recent_sessions", recall)
        self.assertIn("buildRecallSessionOptions", recall)
        self.assertIn("value: session.session_id", recall_sessions)
        self.assertIn("`${displayName}（${groupId}）`", recall_sessions)
        self.assertIn("`${groupChatLabel} ${groupId}`", recall_sessions)
        self.assertNotIn("message_count", recall_sessions)
        self.assertIn("session_id: session || undefined", recall)
        self.assertRegex(
            recall,
            re.compile(
                r"<SelectField\s+.*?id=\"recall-session\".*?label=\{t\('session'\)\}.*?"
                r"value=\{session\}.*?onChange=\{setSession\}",
                re.DOTALL,
            ),
        )
        self.assertNotRegex(
            recall,
            re.compile(r"<input\b[^>]*\bvalue=\{session\}", re.DOTALL),
        )
        self.assertRegex(
            recall,
            re.compile(
                r"<textarea.*?rows=\{5\}.*?required.*?aria-describedby=\"recall-keyboard-hint\"",
                re.DOTALL,
            ),
        )
        self.assertIn('<p id="recall-keyboard-hint" className="recall-keyboard-hint">{t(\'keyboardHint\')}</p>', recall)
        self.assertIn("action={<button className=\"wf-button\" type=\"button\" onClick={() => void run()}>{t('retry')}</button>}", recall)
        self.assertIn("setDetailError('');\n    if (!query.trim()) return", recall)
        self.assertIn("const explicitPercentage = Number(item.score_percentage)", recall)
        self.assertIn("const similarityPercentage = Number(item.similarity_score) * 100", recall)
        self.assertIn("Number.isFinite(explicitPercentage) ? explicitPercentage : similarityPercentage", recall)

        self.assertRegex(
            styles,
            re.compile(
                r"\.recall-workspace \{.*?display: grid;.*?width: min\(1180px, 100%\);.*?margin-inline: auto;",
                re.DOTALL,
            ),
        )
        self.assertIn("grid-template-columns: minmax(0, 1fr) minmax(250px, 300px)", styles)
        self.assertRegex(
            styles,
            re.compile(
                r"\.recall-query textarea \{.*?min-height: 180px;.*?resize: vertical;.*?line-height: 1\.6;",
                re.DOTALL,
            ),
        )
        self.assertRegex(
            styles,
            re.compile(r"@media \(max-width: 900px\) \{.*?\.recall-form \{ grid-template-columns: 1fr; \}", re.DOTALL),
        )
        self.assertRegex(
            styles,
            re.compile(r"@media \(max-width: 760px\) \{.*?\.recall-result-layout \{ grid-template-columns: 1fr; \}", re.DOTALL),
        )
        self.assertRegex(
            styles,
            re.compile(
                r"@media \(max-width: 560px\) \{.*?\.recall-result-summary \{ grid-template-columns: 1fr; \}"
                r".*?\.recall-result-actions \.wf-button \{ width: 100%; \}",
                re.DOTALL,
            ),
        )

    def test_graph_interaction_contracts(self):
        graph = (APP_ROOT / "src" / "features" / "memory" / "GraphPage.tsx").read_text(encoding="utf-8")
        canvas = (APP_ROOT / "src" / "features" / "memory" / "CytoscapeGraphCanvas.tsx").read_text(encoding="utf-8")
        layout = (APP_ROOT / "src" / "features" / "memory" / "graphCytoscape.ts").read_text(encoding="utf-8")
        styles = (APP_ROOT / "src" / "styles.css").read_text(encoding="utf-8")
        i18n = (APP_ROOT / "src" / "i18n.ts").read_text(encoding="utf-8")
        package_sources = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (PLUGIN_ROOT / "web" / "package.json", APP_ROOT / "package.json")
        )

        self.assertIn("CytoscapeGraphCanvas", graph)
        self.assertIn("visibleEdges={visibleEdges}", graph)
        self.assertIn("graphCanvasRef.current?.reflow()", graph)
        self.assertIn('role="toolbar"', graph)
        for contract in (
            "cytoscape({",
            "buildGraphModel",
            "createFcoseLayout",
            "userPanningEnabled: true",
            "userZoomingEnabled: true",
            "cy.on('free', 'node'",
            "lockGraphNode",
            "releaseGraphNode",
            "graph-edge--hidden",
            "graph-focus-node",
            "onClick={() => onSelectNodeRef.current(node.id)}",
            "ResizeObserver",
            "MutationObserver",
            "cy.destroy()",
        ):
            self.assertIn(contract, canvas)

        for contract in (
            "quality: 'default'",
            "randomize: false",
            "animate: options.animate ?? false",
            "nodeDimensionsIncludeLabels: true",
            "idealEdgeLength: safeIdealEdgeLength",
            "nodeRepulsion: safeNodeRepulsion",
            "MAX_DYNAMIC_GRAPH_NODES",
        ):
            self.assertIn(contract, layout)

        for class_name in (
            "graph-cytoscape-viewport",
            "graph-focus-layer",
            "graph-focus-node",
        ):
            self.assertIn(f".{class_name}", styles)
        self.assertIn("cytoscape-graph-canvas", canvas)
        self.assertIn("overscroll-behavior: contain", styles)
        self.assertIn("touch-action: none", styles)
        self.assertIn("pointer-events: none", styles)
        self.assertIn("@media (prefers-reduced-motion: reduce)", styles)
        self.assertRegex(styles, re.compile(r"prefers-reduced-motion: reduce.*?\.graph-focus-node\s*\{\s*transition: none;", re.DOTALL))

        self.assertEqual(i18n.count("reflowGraph:"), 3)
        self.assertEqual(i18n.count("panLeft:"), 3)
        self.assertIn("reflowGraph: '重新布局'", i18n)
        self.assertIn("reflowGraph: 'Reflow layout'", i18n)
        self.assertIn("reflowGraph: 'Перестроить граф'", i18n)
        self.assertRegex(package_sources, re.compile(r'"cytoscape"\s*:\s*"\^3\.34\.0"'))
        self.assertRegex(package_sources, re.compile(r'"cytoscape-fcose"\s*:\s*"\^2\.2\.0"'))
        self.assertNotRegex(package_sources, re.compile(r'"(?:d3(?:-[^"]*)?|force-graph(?:-[^"]*)?)"\s*:'))

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
        for name in ("servers", "trends", "settings", "memory_config", "overview", "memories", "graph", "recall", "source_updates"):
            self.assertIn("useCachedQuery", pages[name], name)
        self.assertNotIn("setData(null)", pages["servers"])
        self.assertNotIn("setData(null)", pages["trends"])
        self.assertIn("if (!cachedData || dirty || saving || pendingPreview) return", pages["settings"])
        self.assertNotIn("配置由后端 Schema 动态生成", pages["memory_config"])
        self.assertNotIn("保存后可能短暂重载整个插件", pages["memory_config"])
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
        self.assertIn("const standalone = !window.AstrBotPluginPage; const memoryAvailable = Boolean(bootstrap?.capabilities.memory.available)", app)
        self.assertNotIn("memoryAvailable = !standalone &&", app)
        self.assertIn("{!memoryAvailable && bootstrap?.capabilities.memory.enabled ?", app)
        self.assertNotIn("{!standalone && !memoryAvailable && bootstrap?.capabilities.memory.enabled ?", app)
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
        styles = (APP_ROOT / "src" / "styles.css").read_text(encoding="utf-8")

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
        self.assertRegex(
            page,
            re.compile(
                r"type FeedbackState =\s*"
                r"\| \{ kind: 'progress'; message: string \}\s*"
                r"\| \{ kind: 'success'; message: string \}\s*"
                r"\| \{ kind: 'warning'; message: string \}",
                re.DOTALL,
            ),
        )
        self.assertIn("useState<FeedbackState | null>(null)", page)
        self.assertRegex(
            page,
            re.compile(
                r"const showFeedback = useCallback\(.*?clearFeedbackTimer\(\);\s*setFeedback\(nextFeedback\);",
                re.DOTALL,
            ),
        )
        self.assertIn("if (feedback?.kind !== 'success') return undefined", page)
        self.assertIn("feedbackTimerRef.current = window.setTimeout(() => {", page)
        self.assertIn("current === successFeedback ? null : current", page)
        self.assertIn("}, 4_000);", page)
        self.assertNotIn("5_000", page)
        self.assertIn("showFeedback({ kind: 'progress', message: '正在保存记忆配置…' })", page)
        self.assertIn("showFeedback({ kind: 'progress', message: '配置已保存，正在等待插件重载…' })", page)
        self.assertIn("setFeedback((current) => current?.kind === 'warning' ? current : null)", page)

        self.assertIn("role={feedback.kind === 'warning' ? 'alert' : 'status'}", page)
        self.assertIn("aria-live={feedback.kind === 'warning' ? undefined : 'polite'}", page)
        self.assertIn("feedback.kind === 'progress' ? '正在处理'", page)
        self.assertIn("feedback.kind === 'success' ? '操作成功' : '需要处理'", page)
        self.assertNotIn("操作完成", page)
        self.assertIn("feedback.kind !== 'progress'", page)
        self.assertIn('aria-label="关闭通知"', page)

        for branch in (
            r"if \(result\.manual_reload_required\).*?kind: 'warning'",
            r"if \(!reloadResult\).*?kind: 'warning'",
            r"if \(reloadResult\.reloadFailed\).*?kind: 'warning'",
        ):
            self.assertRegex(page, re.compile(branch, re.DOTALL))
        self.assertIn("setError(messageOf(reason, '保存记忆配置失败'))", page)
        self.assertIn("setError(`${messageOf(reason, '配置版本冲突')}", page)

        self.assertNotIn('{feedback ? <p className="inline-feedback"', page)
        self.assertNotIn("保存期间请勿重复提交或离开页面", page)
        self.assertNotIn("插件重载期间，独立管理页和 Memory 数据接口可能短暂不可用", page)
        self.assertIn(".memory-config-toast {", styles)
        self.assertIn(".memory-config-toast--warning {", styles)
        self.assertRegex(
            styles,
            re.compile(r"\.memory-config-toast--warning \{\s*border-color: var\(--wf-danger\);"),
        )
        self.assertIn(".memory-config-toast__close:focus-visible", styles)
        self.assertRegex(
            styles,
            re.compile(r"\.memory-config-toast__close \{.*?min-height: 44px;", re.DOTALL),
        )
        self.assertIn(".page-stack,\n  .memory-config-toast {\n    animation: none;", styles)

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
