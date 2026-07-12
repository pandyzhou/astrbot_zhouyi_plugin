import { useCallback, useEffect, useMemo, useState } from 'react';
import { DataState, WorkshopPanel } from '@pandyzhou/astrbot-mc-ui';
import { memoryGet } from '../../api/client';
import { useI18n } from '../../i18n';
import type { BackupItem, StatsData } from './types';

export function OverviewPage() {
  const { t } = useI18n();
  const [stats, setStats] = useState<StatsData | null>(null);
  const [backups, setBackups] = useState<BackupItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);

  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    setError(null);
    try {
      const [nextStats, backupData] = await Promise.all([
        memoryGet<StatsData>('stats', undefined, signal),
        memoryGet<{ backups: BackupItem[] }>('backups', undefined, signal).catch(() => ({ backups: [] })),
      ]);
      setStats(nextStats);
      setBackups(backupData.backups ?? []);
    } catch (reason) {
      if ((reason as Error).name !== 'AbortError') setError(reason as Error);
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  const status = stats?.status_breakdown ?? {};
  const importance = useMemo(() => Object.entries(stats?.importance_distribution ?? {}), [stats]);
  const atoms = useMemo(() => Object.entries(stats?.atom_breakdown ?? {}), [stats]);
  const importanceMax = Math.max(...importance.map(([, value]) => value), 1);
  const atomMax = Math.max(...atoms.map(([, value]) => value), 1);

  if (loading) return <DataState state="loading" title={t('loading')} message={t('memoryCapability')} />;
  if (error) return <DataState state="error" title={t('operationFailed')} message={error.message} action={<button className="wf-button" type="button" onClick={() => void load()}>{t('retry')}</button>} />;

  const cards = [
    [t('total'), stats?.total_memories ?? 0],
    [t('active'), status.active ?? 0],
    [t('archived'), status.archived ?? 0],
    [t('deleted'), status.deleted ?? 0],
    [t('graphNodes'), stats?.graph_nodes ?? 0],
    [t('atoms'), stats?.atom_count ?? 0],
  ];

  return (
    <div className="page-stack">
      <header className="page-heading"><div><p className="eyebrow">MEMORY OPERATIONS</p><h1>{t('overview')}</h1></div><button className="wf-button" type="button" onClick={() => void load()}>{t('refresh')}</button></header>
      <div className="memory-stat-grid">{cards.map(([label, value]) => <article className="summary-card" key={String(label)}><span>{label}</span><strong>{value}</strong></article>)}</div>
      <div className="memory-overview-grid">
        <WorkshopPanel title={t('importanceDistribution')} description="0–10">
          {importance.length ? <div className="bar-list">{importance.map(([label, value]) => <div className="bar-item" key={label}><span>{label}</span><progress aria-label={`${label}: ${value}`} max={importanceMax} value={value} /><strong>{value}</strong></div>)}</div> : <DataState state="empty" title={t('empty')} message={t('noImportanceDistribution')} />}
        </WorkshopPanel>
        <WorkshopPanel title={t('atomBreakdown')}>
          {atoms.length ? <div className="bar-list">{atoms.map(([label, value]) => <div className="bar-item bar-item--wide" key={label}><span>{label}</span><progress aria-label={`${label}: ${value}`} max={atomMax} value={value} /><strong>{value}</strong></div>)}</div> : <DataState state="empty" title={t('empty')} message={t('noAtomBreakdown')} />}
        </WorkshopPanel>
        <WorkshopPanel title={t('sessions')}><div className="dense-list">{stats?.recent_sessions?.length ? stats.recent_sessions.map((item) => <article key={item.session_id}><code>{item.session_id}</code><strong>{item.message_count}</strong></article>) : <p className="muted">{t('empty')}</p>}</div></WorkshopPanel>
        <WorkshopPanel title={t('backups')}><div className="dense-list">{backups.length ? backups.map((item, index) => <article key={`${item.name ?? item.directory}-${index}`}><div><strong>{item.name ?? item.directory ?? 'backup'}</strong><small>{item.backup_timestamp ?? '—'}</small></div><span>{item.file_count ?? item.files_copied ?? 0} {t('files')}</span></article>) : <p className="muted">{t('empty')}</p>}</div></WorkshopPanel>
      </div>
    </div>
  );
}
