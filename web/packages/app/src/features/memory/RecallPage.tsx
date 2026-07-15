import { useState } from 'react';
import { DataState, WorkshopPanel } from '@pandyzhou/astrbot-mc-ui';
import { memoryGet, memoryPost } from '../../api/client';
import { useI18n } from '../../i18n';
import { displayImportance, MemoryDetailDrawer } from './MemoryDetailDrawer';
import type { MemoryDetail, RecallData, RecallItem } from './types';

function formatRawScore(value: unknown) {
  const score = Number(value);
  return Number.isFinite(score) ? score.toFixed(4) : '—';
}

function formatScorePercentage(item: RecallItem) {
  const explicitPercentage = Number(item.score_percentage);
  const similarityPercentage = Number(item.similarity_score) * 100;
  const percentage = Number.isFinite(explicitPercentage) ? explicitPercentage : similarityPercentage;
  return Number.isFinite(percentage) ? `${percentage.toFixed(1)}%` : '—';
}

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
    setDetailError('');
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
      <div className="recall-workspace">
        <WorkshopPanel title={t('hybridRetrieval')}>
          <form className="recall-form" onSubmit={(event) => { event.preventDefault(); void run(); }}>
            <label className="wf-label recall-query">
              {t('query')}
              <textarea
                className="wf-input"
                rows={5}
                required
                aria-describedby="recall-keyboard-hint"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                onKeyDown={(event) => { if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) { event.preventDefault(); void run(); } }}
              />
            </label>
            <div className="recall-options">
              <label className="wf-label">k<input className="wf-input" type="number" min="1" max="50" value={k} onChange={(event) => setK(Math.max(1, Math.min(50, Number(event.target.value) || 1)))} /></label>
              <label className="wf-label">{t('session')}<input className="wf-input" value={session} onChange={(event) => setSession(event.target.value)} /></label>
              <p id="recall-keyboard-hint" className="recall-keyboard-hint">{t('keyboardHint')}</p>
              <button className="wf-button wf-button--primary recall-run-button" disabled={loading || !query.trim()}>{loading ? t('processing') : t('run')}</button>
            </div>
          </form>
        </WorkshopPanel>
        {detailError ? <p className="inline-feedback inline-feedback--error" role="alert">{detailError}</p> : null}
        {loading ? <DataState state="loading" title={t('loading')} message={t('hybridRetrieval')} /> : error ? <DataState state="error" title={t('operationFailed')} message={error.message} action={<button className="wf-button" type="button" onClick={() => void run()}>{t('retry')}</button>} /> : data ? (
          <WorkshopPanel title={t('results')}>
            <dl className="recall-result-summary">
              <div><dt>{t('results')}</dt><dd>{data.total}</dd></div>
              <div><dt>{t('elapsed')}</dt><dd>{data.elapsed_time_ms ?? 0} ms</dd></div>
            </dl>
            {data.results.length ? <div className="recall-results">{data.results.map((item, index) => {
              const status = item.metadata?.status ?? 'active';
              const statusClassName = status === 'active' || status === 'deleted' ? ` status-chip--${status}` : '';
              return (
                <article className="recall-result-card" key={`${item.memory_id}-${index}`}>
                  <header className="recall-result-header">
                    <span className="recall-result-rank">#{index + 1}</span>
                    <code>ID {item.memory_id}</code>
                    <strong className="recall-result-score"><span>{formatScorePercentage(item)}</span><small>{t('score')} {formatRawScore(item.similarity_score)}</small></strong>
                  </header>
                  <div className="recall-result-layout">
                    <p className="recall-result-content">{item.content}</p>
                    <dl className="recall-metadata">
                      <div><dt>{t('type')}</dt><dd><span className="type-chip">{item.metadata?.memory_type ?? 'GENERAL'}</span></dd></div>
                      <div><dt>{t('status')}</dt><dd><span className={`status-chip${statusClassName}`}>{status}</span></dd></div>
                      <div><dt>{t('importance')}</dt><dd>{displayImportance(item.metadata?.importance).toFixed(1)}</dd></div>
                      <div><dt>{t('session')}</dt><dd>{item.metadata?.session_id ?? '—'}</dd></div>
                    </dl>
                  </div>
                  <details className="recall-score-disclosure">
                    <summary>{t('scoreBreakdown')}</summary>
                    <div className="recall-score-disclosure-content">
                      {item.score_breakdown && Object.keys(item.score_breakdown).length ? <dl className="score-breakdown">{Object.entries(item.score_breakdown).map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{Number(value).toFixed(6)}</dd></div>)}</dl> : <p className="muted">{t('empty')}</p>}
                    </div>
                  </details>
                  <footer className="recall-result-actions"><button className="wf-button" type="button" onClick={() => void openDetail(item)}>{t('openFullDetail')}</button></footer>
                </article>
              );
            })}</div> : <DataState state="empty" title={t('empty')} message={t('noRecallResults')} />}
          </WorkshopPanel>
        ) : <DataState state="empty" title={t('recallReady')} message={t('recallPrompt')} />}
      </div>
      {(selectedResult || detailLoading) ? <MemoryDetailDrawer detail={detail} loading={detailLoading} scoreBreakdown={selectedResult?.score_breakdown} onClose={() => { setDetail(null); setSelectedResult(null); }} /> : null}
    </div>
  );
}
