import { useMemo } from 'react';
import { DataState, WorkshopPanel } from '@pandyzhou/astrbot-mc-ui';
import { memoryAdminClient, memoryGet } from '../../api/client';
import { useI18n } from '../../i18n';
import { queryCache } from '../../store/queryCacheCore';
import { queryKeys } from '../../store/queryKeys';
import { useCachedQuery } from '../../store/useCachedQuery';
import type { BackupItem, StatsData } from './types';

export function OverviewPage() {
  const { t } = useI18n();
  const statsQuery = useCachedQuery<StatsData>(
    queryKeys.memoryOverviewStats,
    () => memoryGet<StatsData>('stats'),
  );
  const maintenanceQuery = useCachedQuery(
    queryKeys.memoryMaintenance,
    () => memoryAdminClient.maintenance(),
    { ttl: 15_000 },
  );
  const backupsQuery = useCachedQuery<{ backups: BackupItem[] }>(
    queryKeys.memoryOverviewBackups,
    () => memoryGet<{ backups: BackupItem[] }>('backups'),
    { ttl: 300_000 },
  );
  const stats = statsQuery.data;
  const backups = backupsQuery.data?.backups ?? [];

  const refresh = () => {
    queryCache.invalidate(queryKeys.memoryOverviewStats);
    queryCache.invalidate(queryKeys.memoryOverviewBackups);
    queryCache.invalidate(queryKeys.memoryMaintenance);
    void Promise.allSettled([statsQuery.refresh(), backupsQuery.refresh(), maintenanceQuery.refresh()]);
  };

  const status = stats?.status_breakdown ?? {};
  const importance = useMemo(() => Object.entries(stats?.importance_distribution ?? {}), [stats]);
  const atoms = useMemo(() => Object.entries(stats?.atom_breakdown ?? {}), [stats]);
  const importanceMax = Math.max(...importance.map(([, value]) => value), 1);
  const atomMax = Math.max(...atoms.map(([, value]) => value), 1);

  if (statsQuery.isInitialLoading) return <DataState state="loading" title={t('loading')} message={t('memoryCapability')} />;
  if (!stats && statsQuery.error) return <DataState state="error" title={t('operationFailed')} message={statsQuery.error instanceof Error ? statsQuery.error.message : String(statsQuery.error)} action={<button className="wf-button" type="button" onClick={() => { void statsQuery.refresh().catch(() => undefined); }}>{t('retry')}</button>} />;

  const maintenance = maintenanceQuery.data;
  const cards = [
    [t('total'), stats?.total_memories ?? 0],
    [t('active'), status.active ?? 0],
    ['对象迁移', `${maintenance?.migration.processed ?? 0}/${maintenance?.migration.total ?? 0}`],
    ['未解析 owner', maintenance?.migration.unresolved_owner_count ?? 0],
    ['来源覆盖', `${Math.round((maintenance?.sources.coverage_ratio ?? 0) * 100)}%`],
    ['索引待同步', (maintenance?.index.pending_count ?? 0) + (maintenance?.index.needs_repair_count ?? 0)],
  ];

  return (
    <div className="page-stack">
      <header className="page-heading"><div><p className="eyebrow">MEMORY OPERATIONS</p><h1>{t('overview')}</h1></div><button className="wf-button" type="button" onClick={refresh}>{t('refresh')}</button></header>
      {stats && statsQuery.error ? <p className="inline-feedback inline-feedback--error" role="alert">{statsQuery.error instanceof Error ? statsQuery.error.message : String(statsQuery.error)}</p> : null}
      {backupsQuery.data && backupsQuery.error ? <p className="inline-feedback inline-feedback--error" role="alert">{backupsQuery.error instanceof Error ? backupsQuery.error.message : String(backupsQuery.error)}</p> : null}
      <div className="memory-stat-grid">{cards.map(([label, value]) => <article className="summary-card" key={String(label)}><span>{label}</span><strong>{value}</strong></article>)}</div>
      <div className="memory-overview-grid">
        <WorkshopPanel title={t('importanceDistribution')} description="0–10">
          {importance.length ? <div className="bar-list">{importance.map(([label, value]) => <div className="bar-item" key={label}><span>{label}</span><progress aria-label={`${label}: ${value}`} max={importanceMax} value={value} /><strong>{value}</strong></div>)}</div> : <DataState state="empty" title={t('empty')} message={t('noImportanceDistribution')} />}
        </WorkshopPanel>
        <WorkshopPanel title={t('atomBreakdown')}>
          {atoms.length ? <div className="bar-list">{atoms.map(([label, value]) => <div className="bar-item bar-item--wide" key={label}><span>{label}</span><progress aria-label={`${label}: ${value}`} max={atomMax} value={value} /><strong>{value}</strong></div>)}</div> : <DataState state="empty" title={t('empty')} message={t('noAtomBreakdown')} />}
        </WorkshopPanel>
        <WorkshopPanel title={t('sessions')}><div className="dense-list">{stats?.recent_sessions?.length ? stats.recent_sessions.map((item) => <article key={item.session_id}><code>{item.session_id}</code><strong>{item.message_count}</strong></article>) : <p className="muted">{t('empty')}</p>}</div></WorkshopPanel>
        <WorkshopPanel title={t('backups')}>
          {backupsQuery.isInitialLoading ? <DataState state="loading" title={t('loading')} message={t('backups')} /> : !backupsQuery.data && backupsQuery.error ? <DataState state="error" title={t('operationFailed')} message={backupsQuery.error instanceof Error ? backupsQuery.error.message : String(backupsQuery.error)} /> : <div className="dense-list">{backups.length ? backups.map((item, index) => <article key={`${item.name ?? item.directory}-${index}`}><div><strong>{item.name ?? item.directory ?? 'backup'}</strong><small>{item.backup_timestamp ?? '—'}</small></div><span>{item.file_count ?? item.files_copied ?? 0} {t('files')}</span></article>) : <p className="muted">{t('empty')}</p>}</div>}
        </WorkshopPanel>
      </div>
    </div>
  );
}
