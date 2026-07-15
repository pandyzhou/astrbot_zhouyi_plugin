import { lazy, Suspense, useEffect, useMemo, useRef, useState, type MouseEvent } from 'react';
import { DataState, SelectField, WorkshopShell } from '@pandyzhou/astrbot-mc-ui';
import { HashRouter, Navigate, NavLink, Route, Routes, useLocation } from 'react-router-dom';
import { ApiClientError, apiClient } from './api/client';
import type { BootstrapData } from './api/types';
import { ServersPage } from './features/servers/ServersPage';
import { MemoryConfigPage } from './features/settings/MemoryConfigPage';
import { SettingsPage } from './features/settings/SettingsPage';
import { TrendsPage } from './features/trends/TrendsPage';
import { SourceUpdatesPage } from './features/sources/SourceUpdatesPage';
import { MemoriesPage } from './features/memory/MemoriesPage';
import { OverviewPage } from './features/memory/OverviewPage';
import { RecallPage } from './features/memory/RecallPage';
import { I18nContext, type Locale, translate } from './i18n';
import { initializeGroups, useWorkshopStore } from './store/workshopStore';

const GraphPage = lazy(() => import('./features/memory/GraphPage'));
type Theme = 'dark' | 'light';

function AppContent() {
  const location = useLocation();
  const groups = useWorkshopStore((state) => state.groups); const selectedGroupId = useWorkshopStore((state) => state.selectedGroupId); const selectGroup = useWorkshopStore((state) => state.selectGroup);
  const [bootstrap, setBootstrap] = useState<BootstrapData | null>(null); const [loading, setLoading] = useState(true); const [error, setError] = useState<Error | null>(null);
  const browserLanguage = navigator.language.slice(0, 2); const locale: Locale = browserLanguage === 'ru' ? 'ru' : browserLanguage === 'en' ? 'en' : 'zh';
  const [theme, setTheme] = useState<Theme>(() => localStorage.getItem('zhouyi-dashboard-theme') === 'light' ? 'light' : 'dark');
  const [settingsNavigationLocked, setSettingsNavigationLocked] = useState(false);
  const [memoryConfigNavigationLocked, setMemoryConfigNavigationLocked] = useState(false);
  const navigationLocked = settingsNavigationLocked || memoryConfigNavigationLocked;
  const acceptedHashRef = useRef(window.location.hash);
  const approvedHashRef = useRef<{ hash: string; expiresAt: number } | null>(null);
  const restoringHashRef = useRef<string | null>(null);
  const t = useMemo(() => (key: string) => translate(locale, key), [locale]);
  const standalone = !window.AstrBotPluginPage; const memoryAvailable = Boolean(bootstrap?.capabilities.memory.available); const mcRoute = location.pathname.startsWith('/mc/');
  useEffect(() => { document.documentElement.dataset.theme = theme; localStorage.setItem('zhouyi-dashboard-theme', theme); }, [theme]);
  useEffect(() => { document.documentElement.lang = locale === 'zh' ? 'zh-CN' : locale; }, [locale]);
  useEffect(() => { const controller = new AbortController(); setLoading(true); setError(null); apiClient.bootstrap(controller.signal).then((data) => { setBootstrap(data); initializeGroups(data.groups, data.default_group_id); }).catch((reason: unknown) => { if ((reason as Error).name !== 'AbortError') setError(reason instanceof Error ? reason : new Error('Bootstrap failed')); }).finally(() => { if (!controller.signal.aborted) setLoading(false); }); return () => controller.abort(); }, []);
  useEffect(() => {
    if (approvedHashRef.current?.hash === window.location.hash) approvedHashRef.current = null;
    if (!navigationLocked) acceptedHashRef.current = window.location.hash;
  }, [location.pathname, navigationLocked]);
  useEffect(() => {
    const handleHashChange = () => {
      const nextHash = window.location.hash;
      if (restoringHashRef.current === nextHash) {
        restoringHashRef.current = null;
        approvedHashRef.current = null;
        acceptedHashRef.current = nextHash;
        return;
      }
      if (nextHash === acceptedHashRef.current) return;
      const approved = approvedHashRef.current;
      if (approved?.hash === nextHash && approved.expiresAt >= Date.now()) {
        approvedHashRef.current = null;
        acceptedHashRef.current = nextHash;
        return;
      }
      approvedHashRef.current = null;
      if (!navigationLocked || window.confirm('当前页面有未保存或正在处理的配置更改，确定离开吗？')) {
        acceptedHashRef.current = nextHash;
        return;
      }
      restoringHashRef.current = acceptedHashRef.current;
      window.location.hash = acceptedHashRef.current;
    };
    window.addEventListener('hashchange', handleHashChange);
    return () => window.removeEventListener('hashchange', handleHashChange);
  }, [navigationLocked]);
  const authRequired = standalone && error instanceof ApiClientError && error.code === 'AUTH_REQUIRED'; const dashboardLoginUrl = `https://${window.location.hostname}:35015/#/auth/login`;
  function handleNavigationClick(event: MouseEvent<HTMLDivElement>) {
    const anchor = (event.target as Element).closest('a');
    if (!anchor || !navigationLocked || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const targetHash = new URL((anchor as HTMLAnchorElement).href, window.location.href).hash;
    if (!targetHash || targetHash === window.location.hash) return;
    if (!window.confirm('当前页面有未保存或正在处理的配置更改，确定离开吗？')) {
      event.preventDefault();
      return;
    }
    approvedHashRef.current = { hash: targetHash, expiresAt: Date.now() + 1_000 };
  }
  const controls = <div className="dashboard-controls">{mcRoute ? <SelectField className="wf-select-field--inline" id="global-group" label="group_id" options={groups.map((group) => ({ value: group.group_id, label: `${group.label} (${group.group_id})` }))} value={selectedGroupId} placeholder="—" disabled={loading || groups.length === 0 || navigationLocked} onChange={selectGroup} /> : null}<button className="wf-button wf-button--quiet theme-button" type="button" onClick={() => setTheme((value) => value === 'dark' ? 'light' : 'dark')}>{theme === 'dark' ? t('light') : t('dark')}</button></div>;
  const defaultPath = memoryAvailable ? '/memory/overview' : '/mc/servers';
  return <I18nContext.Provider value={{ locale, t }}><WorkshopShell brand="Zhouyi Dashboard" groupControl={controls} navigation={<div className="dashboard-navigation" onClickCapture={handleNavigationClick}>{memoryAvailable ? <><NavLink to="/memory/overview">{t('overview')}</NavLink><NavLink to="/memory/memories">{t('memories')}</NavLink><NavLink to="/memory/recall">{t('recall')}</NavLink><NavLink to="/memory/graph">{t('graph')}</NavLink></> : null}<NavLink to="/settings/memory">{t('memoryConfig')}</NavLink><NavLink to="/mc/servers">{t('servers')}</NavLink><NavLink to="/mc/trends">{t('trends')}</NavLink><NavLink to="/mc/settings">{t('settings')}</NavLink><NavLink to="/sources/updates">{t('sourceUpdates')}</NavLink></div>}>
    {loading ? <DataState state="loading" title={t('loading')} message="Zhouyi Dashboard bootstrap" /> : null}
    {!loading && error ? <DataState state="error" title={authRequired ? 'AstrBot authentication required' : t('operationFailed')} message={error.message} action={authRequired ? <div className="auth-required-actions"><a className="wf-button" href={dashboardLoginUrl} target="_blank" rel="noopener noreferrer">AstrBot Login</a><button className="wf-button" onClick={() => window.location.reload()}>{t('retry')}</button></div> : undefined} /> : null}
    {!loading && !error ? <>{!memoryAvailable && bootstrap?.capabilities.memory.enabled ? <p className="capability-banner">{t('memoryUnavailable')}: {bootstrap.capabilities.memory.error ?? bootstrap.capabilities.memory.reason ?? t('notInitialized')}</p> : null}<Routes><Route path="/mc/servers" element={selectedGroupId ? <ServersPage /> : <DataState state="empty" title={t('empty')} message="group_id" />} /><Route path="/mc/trends" element={selectedGroupId ? <TrendsPage /> : <DataState state="empty" title={t('empty')} message="group_id" />} /><Route path="/mc/settings" element={selectedGroupId ? <SettingsPage onNavigationLockChange={setSettingsNavigationLocked} /> : <DataState state="empty" title={t('empty')} message="group_id" />} /><Route path="/settings/memory" element={<MemoryConfigPage onNavigationLockChange={setMemoryConfigNavigationLocked} />} />{memoryAvailable ? <><Route path="/memory/overview" element={<OverviewPage />} /><Route path="/memory/memories" element={<MemoriesPage />} /><Route path="/memory/recall" element={<RecallPage />} /><Route path="/memory/graph" element={<Suspense fallback={<DataState state="loading" title={t('loading')} message="Graph module" />}><GraphPage /></Suspense>} /></> : null}<Route path="/sources/updates" element={<SourceUpdatesPage />} /><Route path="*" element={<Navigate replace to={defaultPath} />} /></Routes></> : null}
  </WorkshopShell></I18nContext.Provider>;
}

export function App() { return <HashRouter><AppContent /></HashRouter>; }
