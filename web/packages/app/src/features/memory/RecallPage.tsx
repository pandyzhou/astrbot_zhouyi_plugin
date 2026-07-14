import { useState } from 'react';
import { DataState, WorkshopPanel } from '@pandyzhou/astrbot-mc-ui';
import { memoryGet, memoryPost } from '../../api/client';
import { useI18n } from '../../i18n';
import { displayImportance, MemoryDetailDrawer } from './MemoryDetailDrawer';
import type { MemoryDetail, RecallData, RecallItem } from './types';

export function RecallPage() {
  const { t } = useI18n();
  const [query, setQuery] = useState('');
  const [session, setSession] = useState('');
  const [k, setK] = useState(5);
  const [data, setData] = useState<RecallData | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<Error | null>(null);
  const [detail, setDetail] = useState<MemoryDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState('');
  const [selectedResult, setSelectedResult] = useState<RecallItem | null>(null);

  const run = async () => {
    if (!query.trim()) return;
    setLoading(true);
    setError(null);
    try {
      setData(await memoryPost<RecallData>('recall/test', { query: query.trim(), k, session_id: session || undefined }, undefined, `recall:${query}:${k}:${session}`));
    } catch (reason) {
      setError(reason as Error);
      setData(null);
    } finally {
      setLoading(false);
    }
  };

  const openDetail = async (item: RecallItem) => {
    const memoryId = Number(item.memory_id);
    if (!Number.isInteger(memoryId)) {
      setDetailError(t('invalidMemoryId'));
      return;
    }
    setSelectedResult(item);
    setDetail(null);
    setDetailError('');
    setDetailLoading(true);
    try {
      setDetail(await memoryGet<MemoryDetail>('memories/detail', { memory_id: memoryId }));
    } catch (reason) {
      setDetailError((reason as Error).message);
      setSelectedResult(null);
    } finally {
      setDetailLoading(false);
    }
  };

  return (
    <div className="page-stack">
      <header className="page-heading"><div><p className="eyebrow">HYBRID RETRIEVAL</p><h1>{t('recall')}</h1></div></header>
      <WorkshopPanel title={t('query')}>
        <form className="recall-form" onSubmit={(event) => { event.preventDefault(); void run(); }}>
          <label className="wf-label recall-query">{t('query')}<textarea className="wf-input" rows={4} value={query} onChange={(event) => setQuery(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) { event.preventDefault(); void run(); } }} /></label>
          <label className="wf-label">k<input className="wf-input" type="number" min="1" max="50" value={k} onChange={(event) => setK(Math.max(1, Math.min(50, Number(event.target.value) || 1)))} /></label>
          <label className="wf-label">{t('session')}<input className="wf-input" value={session} onChange={(event) => setSession(event.target.value)} /></label>
          <button className="wf-button wf-button--primary" disabled={loading || !query.trim()}>{loading ? t('processing') : t('run')}</button>
        </form>
      </WorkshopPanel>
      {detailError ? <p className="inline-feedback inline-feedback--error" role="alert">{detailError}</p> : null}
      {loading ? <DataState state="loading" title={t('loading')} message={t('hybridRetrieval')} /> : error ? <DataState state="error" title={t('operationFailed')} message={error.message} /> : data ? (
        <WorkshopPanel title={`${t('results')} · ${data.total}`} description={`${t('elapsed')} ${data.elapsed_time_ms ?? 0} ms`}>
          {data.results.length ? <div className="recall-results">{data.results.map((item, index) => (
            <article key={`${item.memory_id}-${index}`}>
              <header><span>#{index + 1}</span><code>ID {item.memory_id}</code><strong>{t('score')} {Number(item.similarity_score ?? 0).toFixed(4)}</strong></header>
              <p>{item.content}</p>
              <dl className="recall-metadata">
                <div><dt>{t('type')}</dt><dd>{item.metadata?.memory_type ?? 'GENERAL'}</dd></div>
                <div><dt>{t('status')}</dt><dd>{item.metadata?.status ?? 'active'}</dd></div>
                <div><dt>{t('importance')}</dt><dd>{displayImportance(item.metadata?.importance).toFixed(1)}</dd></div>
                <div><dt>{t('session')}</dt><dd>{item.metadata?.session_id ?? '—'}</dd></div>
              </dl>
              <section className="recall-score-section" aria-label={t('scoreBreakdown')}>
                <h3>{t('scoreBreakdown')}</h3>
                {item.score_breakdown && Object.keys(item.score_breakdown).length ? <dl className="score-breakdown">{Object.entries(item.score_breakdown).map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{Number(value).toFixed(6)}</dd></div>)}</dl> : <p className="muted">{t('empty')}</p>}
              </section>
              <footer><button className="wf-button" type="button" onClick={() => void openDetail(item)}>{t('openFullDetail')}</button></footer>
            </article>
          ))}</div> : <DataState state="empty" title={t('empty')} message={t('noRecallResults')} />}
        </WorkshopPanel>
      ) : <DataState state="empty" title={t('recallReady')} message={t('recallPrompt')} />}
      {(selectedResult || detailLoading) ? <MemoryDetailDrawer detail={detail} loading={detailLoading} scoreBreakdown={selectedResult?.score_breakdown} onClose={() => { setDetail(null); setSelectedResult(null); }} /> : null}
    </div>
  );
}
