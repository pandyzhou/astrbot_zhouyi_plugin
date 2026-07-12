import { lazy, Suspense, useEffect, useMemo, useState } from 'react';
import { DataState, SelectField, WorkshopShell } from '@pandyzhou/astrbot-mc-ui';
import { HashRouter, Navigate, NavLink, Route, Routes, useLocation } from 'react-router-dom';
import { ApiClientError, apiClient } from './api/client';
import type { BootstrapData } from './api/types';
import { ServersPage } from './features/servers/ServersPage';
import { SettingsPage } from './features/settings/SettingsPage';
import { TrendsPage } from './features/trends/TrendsPage';
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
  const [locale, setLocale] = useState<Locale>(() => { const saved = localStorage.getItem('zhouyi-dashboard-locale'); const browser = navigator.language.slice(0, 2); return (saved === 'zh' || saved === 'en' || saved === 'ru') ? saved : (browser === 'ru' ? 'ru' : browser === 'en' ? 'en' : 'zh'); });
  const [theme, setTheme] = useState<Theme>(() => localStorage.getItem('zhouyi-dashboard-theme') === 'light' ? 'light' : 'dark');
  const t = useMemo(() => (key: string) => translate(locale, key), [locale]);
  const standalone = !window.AstrBotPluginPage; const memoryAvailable = !standalone && Boolean(bootstrap?.capabilities.memory.available); const mcRoute = location.pathname.startsWith('/mc/');
  useEffect(() => { document.documentElement.dataset.theme = theme; localStorage.setItem('zhouyi-dashboard-theme', theme); }, [theme]);
  useEffect(() => { document.documentElement.lang = locale === 'zh' ? 'zh-CN' : locale; localStorage.setItem('zhouyi-dashboard-locale', locale); }, [locale]);
  useEffect(() => { const controller = new AbortController(); setLoading(true); setError(null); apiClient.bootstrap(controller.signal).then((data) => { setBootstrap(data); initializeGroups(data.groups, data.default_group_id); }).catch((reason: unknown) => { if ((reason as Error).name !== 'AbortError') setError(reason instanceof Error ? reason : new Error('Bootstrap failed')); }).finally(() => { if (!controller.signal.aborted) setLoading(false); }); return () => controller.abort(); }, []);
  const authRequired = standalone && error instanceof ApiClientError && error.code === 'AUTH_REQUIRED'; const dashboardLoginUrl = `https://${window.location.hostname}:35015/#/auth/login`;
  const controls = <div className="dashboard-controls">{mcRoute ? <SelectField className="wf-select-field--inline" id="global-group" label="group_id" options={groups.map((group) => ({ value: group.group_id, label: `${group.label} (${group.group_id})` }))} value={selectedGroupId} placeholder="—" disabled={loading || groups.length === 0} onChange={selectGroup} /> : null}<label className="compact-control"><span>{t('language')}</span><select value={locale} onChange={(event) => setLocale(event.target.value as Locale)}><option value="zh">中文</option><option value="en">EN</option><option value="ru">RU</option></select></label><button className="wf-button wf-button--quiet theme-button" type="button" onClick={() => setTheme((value) => value === 'dark' ? 'light' : 'dark')}>{theme === 'dark' ? t('light') : t('dark')}</button></div>;
  const defaultPath = memoryAvailable ? '/memory/overview' : '/mc/servers';
  return <I18nContext.Provider value={{ locale, t }}><WorkshopShell brand="Zhouyi Dashboard" groupControl={controls} navigation={<>{memoryAvailable ? <><NavLink to="/memory/overview">{t('overview')}</NavLink><NavLink to="/memory/memories">{t('memories')}</NavLink><NavLink to="/memory/recall">{t('recall')}</NavLink><NavLink to="/memory/graph">{t('graph')}</NavLink></> : null}<NavLink to="/mc/servers">{t('servers')}</NavLink><NavLink to="/mc/trends">{t('trends')}</NavLink><NavLink to="/mc/settings">{t('settings')}</NavLink></>}>
    {loading ? <DataState state="loading" title={t('loading')} message="Zhouyi Dashboard bootstrap" /> : null}
    {!loading && error ? <DataState state="error" title={authRequired ? 'AstrBot authentication required' : t('operationFailed')} message={error.message} action={authRequired ? <div className="auth-required-actions"><a className="wf-button" href={dashboardLoginUrl} target="_blank" rel="noopener noreferrer">AstrBot Login</a><button className="wf-button" onClick={() => window.location.reload()}>{t('retry')}</button></div> : undefined} /> : null}
    {!loading && !error ? <>{standalone ? <p className="capability-banner">{t('standalone')}</p> : !memoryAvailable && bootstrap?.capabilities.memory.enabled ? <p className="capability-banner">{t('memoryUnavailable')}: {bootstrap.capabilities.memory.error ?? bootstrap.capabilities.memory.reason ?? t('notInitialized')}</p> : null}<Routes><Route path="/mc/servers" element={selectedGroupId ? <ServersPage /> : <DataState state="empty" title={t('empty')} message="group_id" />} /><Route path="/mc/trends" element={selectedGroupId ? <TrendsPage /> : <DataState state="empty" title={t('empty')} message="group_id" />} /><Route path="/mc/settings" element={selectedGroupId ? <SettingsPage /> : <DataState state="empty" title={t('empty')} message="group_id" />} />{memoryAvailable ? <><Route path="/memory/overview" element={<OverviewPage />} /><Route path="/memory/memories" element={<MemoriesPage />} /><Route path="/memory/recall" element={<RecallPage />} /><Route path="/memory/graph" element={<Suspense fallback={<DataState state="loading" title={t('loading')} message="Graph module" />}><GraphPage /></Suspense>} /></> : null}<Route path="*" element={<Navigate replace to={defaultPath} />} /></Routes></> : null}
  </WorkshopShell></I18nContext.Provider>;
}

export function App() { return <HashRouter><AppContent /></HashRouter>; }
