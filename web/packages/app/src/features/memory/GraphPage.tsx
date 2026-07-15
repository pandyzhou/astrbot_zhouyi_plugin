import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { DataState, WorkshopPanel } from '@pandyzhou/astrbot-mc-ui';
import { memoryGet, memoryPost } from '../../api/client';
import { useI18n } from '../../i18n';
import { queryCache } from '../../store/queryCacheCore';
import { queryKeys } from '../../store/queryKeys';
import { useCachedQuery } from '../../store/useCachedQuery';
import {
  CytoscapeGraphCanvas,
  type CytoscapeGraphCanvasHandle,
} from './CytoscapeGraphCanvas';
import { GRAPH_RELATION_COLORS } from './graphCytoscape';
import { displayImportance, MemoryDetailDrawer } from './MemoryDetailDrawer';
import type { GraphMemory, GraphPayload, GraphNode, MemoryDetail } from './types';

export default function GraphPage() {
  const { t } = useI18n();
  const [query, setQuery] = useState('');
  const [session, setSession] = useState('');
  const [persona, setPersona] = useState('');
  const [memoryId, setMemoryId] = useState('');
  const [overviewFilters, setOverviewFilters] = useState({ session: '', persona: '' });
  const overviewKey = useMemo(
    () => queryKeys.memoryGraphOverview(overviewFilters.session || undefined, overviewFilters.persona || undefined),
    [overviewFilters.persona, overviewFilters.session],
  );
  const overviewToken = `${overviewFilters.session}\u0000${overviewFilters.persona}`;
  const initialOverview = queryCache.peek<GraphPayload>(queryKeys.memoryGraphOverview(undefined, undefined))?.data ?? null;
  const overviewQuery = useCachedQuery<GraphPayload>(
    overviewKey,
    () => memoryGet<GraphPayload>('graph/overview', {
      session_id: overviewFilters.session || undefined,
      persona_id: overviewFilters.persona || undefined,
    }),
  );
  const [viewMode, setViewMode] = useState<'overview' | 'custom'>('overview');
  const [data, setData] = useState<GraphPayload | null>(initialOverview);
  const [actionLoading, setActionLoading] = useState(false);
  const [error, setError] = useState<Error | null>(null);
  const [selectedNodeId, setSelectedNodeId] = useState<number | null>(null);
  const [selectedMemoryId, setSelectedMemoryId] = useState<number | null>(null);
  const [activeRelations, setActiveRelations] = useState<Set<string> | null>(null);
  const [graphZoom, setGraphZoom] = useState(1);
  const [detail, setDetail] = useState<MemoryDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState('');
  const graphCanvasRef = useRef<CytoscapeGraphCanvasHandle>(null);
  const viewModeRef = useRef<'overview' | 'custom'>('overview');
  const appliedOverviewKeyRef = useRef(initialOverview ? '\u0000' : '');
  viewModeRef.current = viewMode;

  const applyData = useCallback((next: GraphPayload | null) => {
    setData(next);
    setSelectedNodeId(null);
    setSelectedMemoryId(next?.memory_id ?? null);
    setActiveRelations(null);
    setGraphZoom(1);
  }, []);

  useEffect(() => {
    const next = overviewQuery.data;
    if (!next || viewModeRef.current !== 'overview') return;
    if (appliedOverviewKeyRef.current !== overviewToken) {
      appliedOverviewKeyRef.current = overviewToken;
      applyData(next);
      return;
    }
    setData(next);
  }, [applyData, overviewQuery.data, overviewToken]);

  const overview = () => {
    const nextFilters = { session: session.trim(), persona: persona.trim() };
    const nextToken = `${nextFilters.session}\u0000${nextFilters.persona}`;
    const sameKey = nextToken === overviewToken;
    const nextKey = queryKeys.memoryGraphOverview(nextFilters.session || undefined, nextFilters.persona || undefined);
    const cached = sameKey ? overviewQuery.data : queryCache.peek<GraphPayload>(nextKey)?.data;

    viewModeRef.current = 'overview';
    setViewMode('overview');
    setError(null);
    if (!sameKey) setOverviewFilters(nextFilters);
    if (!sameKey || viewModeRef.current !== viewMode) {
      appliedOverviewKeyRef.current = cached ? nextToken : '';
      applyData(cached ?? null);
    }
    if (sameKey) void overviewQuery.refresh().catch(() => undefined);
  };

  const search = async (focus = false) => {
    viewModeRef.current = 'custom';
    setViewMode('custom');
    setActionLoading(true);
    setError(null);
    try {
      applyData(await memoryPost<GraphPayload>('graph/query', focus
        ? { memory_id: Number(memoryId), session_id: session || undefined, persona_id: persona || undefined }
        : { query: query.trim(), session_id: session || undefined, persona_id: persona || undefined }));
    } catch (reason) {
      setError(reason as Error);
    } finally {
      setActionLoading(false);
    }
  };

  const nodes = data?.snapshot.nodes ?? [];
  const allEdges = data?.snapshot.edges ?? [];
  const relationTypes = useMemo(
    () => [...new Set((data?.snapshot.edges ?? []).map((edge) => edge.relation_type || 'related'))].sort(),
    [data],
  );
  const relationColors = useMemo(
    () => new Map(relationTypes.map((relation, index) => [relation, GRAPH_RELATION_COLORS[index % GRAPH_RELATION_COLORS.length]])),
    [relationTypes],
  );
  const visibleEdges = useMemo(
    () => (data?.snapshot.edges ?? []).filter((edge) => !activeRelations || activeRelations.has(edge.relation_type || 'related')),
    [activeRelations, data],
  );
  const selectedNode = data?.snapshot.nodes.find((node) => node.id === selectedNodeId) ?? null;

  const memoryMap = useMemo(() => {
    const map = new Map<number, GraphMemory>();
    for (const item of [...(data?.snapshot.memories ?? []), ...(data?.top_memories ?? [])]) {
      map.set(item.memory_id, { ...map.get(item.memory_id), ...item });
    }
    for (const item of data?.retrieval.items ?? []) {
      map.set(item.memory_id, { ...map.get(item.memory_id), memory_id: item.memory_id, content: item.content, retrieval: item });
    }
    return map;
  }, [data]);
  const memories = [...memoryMap.values()];
  const selectedMemory = selectedMemoryId === null ? null : memoryMap.get(selectedMemoryId) ?? null;
  const selectedNodeEntries = selectedNodeId === null ? [] : (data?.snapshot.entries ?? []).filter((entry) => entry.node_ids.includes(selectedNodeId));
  const selectedMemoryEntries = selectedMemoryId === null ? [] : (data?.snapshot.entries ?? []).filter((entry) => entry.memory_id === selectedMemoryId);
  const selectedNodeMemoryIds = new Set(selectedNodeEntries.map((entry) => entry.memory_id));
  const selectedMemoryNodeIds = new Set(selectedMemoryEntries.flatMap((entry) => entry.node_ids));
  const highlightedNodeIds = new Set<number>(data?.matched_node_ids ?? []);
  if (selectedNodeId !== null) highlightedNodeIds.add(selectedNodeId);
  selectedMemoryNodeIds.forEach((id) => highlightedNodeIds.add(id));
  const highlightedMemoryIds = new Set<number>(data?.matched_memory_ids ?? []);
  if (selectedMemoryId !== null) highlightedMemoryIds.add(selectedMemoryId);
  selectedNodeMemoryIds.forEach((id) => highlightedMemoryIds.add(id));

  const selectNode = (nodeId: number) => {
    setSelectedNodeId(nodeId);
    setSelectedMemoryId(null);
  };
  const selectMemory = (nextMemoryId: number) => {
    setSelectedMemoryId(nextMemoryId);
    setSelectedNodeId(null);
  };

  const openFullDetail = async (nextMemoryId: number) => {
    setDetail(null);
    setDetailError('');
    setDetailLoading(true);
    try {
      setDetail(await memoryGet<MemoryDetail>('memories/detail', { memory_id: nextMemoryId }));
    } catch (reason) {
      setDetailError((reason as Error).message);
    } finally {
      setDetailLoading(false);
    }
  };

  const toggleRelation = (relation: string) => {
    setActiveRelations((current) => {
      const next = new Set(current ?? relationTypes);
      if (next.has(relation)) next.delete(relation);
      else next.add(relation);
      return next;
    });
  };

  const displayError = viewMode === 'overview' ? overviewQuery.error : error;
  const loading = viewMode === 'overview' ? !data && overviewQuery.isInitialLoading : actionLoading;
  const controlsBusy = actionLoading || (viewMode === 'overview' && overviewQuery.isInitialLoading);

  return (
    <div className="page-stack">
      <header className="page-heading"><div><p className="eyebrow">GRAPH MEMORY</p><h1>{t('graph')}</h1></div></header>
      <WorkshopPanel title={t('filters')}>
        <form className="graph-toolbar" onSubmit={(event) => { event.preventDefault(); void search(); }}>
          <label className="wf-label">{t('query')}<input className="wf-input" value={query} onChange={(event) => setQuery(event.target.value)} /></label>
          <label className="wf-label">{t('session')}<input className="wf-input" value={session} onChange={(event) => setSession(event.target.value)} /></label>
          <label className="wf-label">{t('persona')}<input className="wf-input" value={persona} onChange={(event) => setPersona(event.target.value)} /></label>
          <button className="wf-button wf-button--primary" disabled={controlsBusy}>{t('search')}</button>
          <label className="wf-label">{t('memoryId')}<input className="wf-input" inputMode="numeric" value={memoryId} onChange={(event) => setMemoryId(event.target.value)} /></label>
          <button className="wf-button" type="button" disabled={controlsBusy || !/^\d+$/.test(memoryId)} onClick={() => void search(true)}>{t('focus')}</button>
          <button className="wf-button" type="button" disabled={controlsBusy} onClick={overview}>{t('recent')}</button>
        </form>
      </WorkshopPanel>
      {detailError ? <p className="inline-feedback inline-feedback--error" role="alert">{detailError}</p> : null}
      {data && displayError ? <p className="inline-feedback inline-feedback--error" role="alert">{displayError instanceof Error ? displayError.message : String(displayError)}</p> : null}
      {loading ? <DataState state="loading" title={t('loading')} message={t('graphSnapshot')} /> : !data && displayError ? <DataState state="error" title={t('operationFailed')} message={displayError instanceof Error ? displayError.message : String(displayError)} action={viewMode === 'overview' ? <button className="wf-button" type="button" onClick={overview}>{t('retry')}</button> : undefined} /> : !data?.enabled ? <DataState state="empty" title={t('disabled')} message={t('memoryUnavailable')} /> : !nodes.length ? <DataState state="empty" title={t('graphEmpty')} message={t('graphSnapshot')} /> : (
        <div className="graph-layout">
          <WorkshopPanel title={`${data.mode} · ${data.summary.visible_node_count} ${t('nodes')}`} description={`${visibleEdges.length} ${t('edges')} · ${data.summary.visible_memory_count} ${t('memoriesLabel')}`}>
            <div className="graph-view-controls" role="toolbar" aria-label={t('graphControls')}>
              <button className="wf-button" type="button" aria-label={t('zoomOut')} onClick={() => graphCanvasRef.current?.zoomOut()}>−</button>
              <output aria-live="polite">{Math.round(graphZoom * 100)}%</output>
              <button className="wf-button" type="button" aria-label={t('zoomIn')} onClick={() => graphCanvasRef.current?.zoomIn()}>+</button>
              <button className="wf-button" type="button" aria-label={t('panLeft')} onClick={() => graphCanvasRef.current?.panBy(-48, 0)}>←</button>
              <button className="wf-button" type="button" aria-label={t('panUp')} onClick={() => graphCanvasRef.current?.panBy(0, -48)}>↑</button>
              <button className="wf-button" type="button" aria-label={t('panDown')} onClick={() => graphCanvasRef.current?.panBy(0, 48)}>↓</button>
              <button className="wf-button" type="button" aria-label={t('panRight')} onClick={() => graphCanvasRef.current?.panBy(48, 0)}>→</button>
              <button className="wf-button" type="button" onClick={() => graphCanvasRef.current?.reset()}>{t('resetView')}</button>
              <button className="wf-button" type="button" onClick={() => graphCanvasRef.current?.reflow()}>{t('reflowGraph')}</button>
            </div>
            <CytoscapeGraphCanvas
              ref={graphCanvasRef}
              nodes={nodes}
              allEdges={allEdges}
              visibleEdges={visibleEdges}
              selectedNodeId={selectedNodeId}
              selectedMemoryId={selectedMemoryId}
              highlightedNodeIds={highlightedNodeIds}
              relationColors={relationColors}
              onSelectNode={selectNode}
              onZoomChange={setGraphZoom}
              ariaLabel={`${t('graph')}. ${t('graphKeyboardHint')}`}
              nodeAriaLabel={(node) => `${node.label || node.id}, ${node.type || t('unknown')}`}
            />
            <div className="graph-legend" aria-label={t('legend')}>
              {relationTypes.map((relation) => <button className="legend-item" type="button" key={relation} aria-pressed={!activeRelations || activeRelations.has(relation)} onClick={() => toggleRelation(relation)}><span style={{ background: relationColors.get(relation) }} />{relation} <strong>{data.summary.relation_breakdown[relation] ?? 0}</strong></button>)}
              <span className="legend-node legend-node--matched">{t('matched')}</span><span className="legend-node legend-node--selected">{t('selectedLabel')}</span>
            </div>
          </WorkshopPanel>
          <aside className="graph-sidebar">
            <WorkshopPanel title={selectedNode ? t('nodeDetails') : selectedMemory ? t('memoryDetails') : t('graphSummary')}>
              <div className="graph-inspector">
                {selectedNode ? <NodeInspector node={selectedNode} memoryIds={[...selectedNodeMemoryIds]} memoryMap={memoryMap} onSelectMemory={selectMemory} t={t} /> : selectedMemory ? <MemoryInspector memory={selectedMemory} entries={selectedMemoryEntries} nodes={data.snapshot.nodes.filter((node) => selectedMemoryNodeIds.has(node.id))} onSelectNode={selectNode} onOpenDetail={openFullDetail} t={t} /> : <GraphSummaryView data={data} onSelectNode={selectNode} onSelectMemory={selectMemory} t={t} />}
              </div>
            </WorkshopPanel>
            <WorkshopPanel title={`${t('memoriesLabel')} · ${memories.length}`}>
              <div className="graph-memory-list">{memories.length ? memories.map((memory) => <button type="button" key={memory.memory_id} className={highlightedMemoryIds.has(memory.memory_id) ? 'is-highlighted' : ''} aria-pressed={selectedMemoryId === memory.memory_id} onClick={() => selectMemory(memory.memory_id)}><strong>#{memory.memory_id}</strong><span>{memory.summary || memory.content || t('empty')}</span>{memory.retrieval?.final_score !== undefined ? <small>{t('score')} {memory.retrieval.final_score.toFixed(4)}</small> : null}</button>) : <p className="muted">{t('empty')}</p>}</div>
            </WorkshopPanel>
          </aside>
        </div>
      )}
      {(detail || detailLoading) ? <MemoryDetailDrawer detail={detail} loading={detailLoading} onClose={() => setDetail(null)} /> : null}
    </div>
  );
}

function NodeInspector({ node, memoryIds, memoryMap, onSelectMemory, t }: { node: GraphNode; memoryIds: number[]; memoryMap: Map<number, GraphMemory>; onSelectMemory: (id: number) => void; t: (key: string) => string }) {
  return <div className="detail-stack"><h3>{node.label || `#${node.id}`}</h3><dl><div><dt>ID</dt><dd>{node.id}</dd></div><div><dt>{t('type')}</dt><dd>{node.type ?? t('unknown')}</dd></div><div><dt>{t('weight')}</dt><dd>{Number(node.weight ?? 0).toFixed(2)}</dd></div><div><dt>{t('degree')}</dt><dd>{node.degree ?? 0}</dd></div><div><dt>{t('entries')}</dt><dd>{node.entry_count ?? 0}</dd></div><div><dt>{t('memoriesLabel')}</dt><dd>{node.memory_count ?? memoryIds.length}</dd></div></dl><h3>{t('relatedMemories')}</h3><div className="graph-linked-list">{memoryIds.length ? memoryIds.map((id) => <button className="wf-button" type="button" key={id} onClick={() => onSelectMemory(id)}>#{id} {memoryMap.get(id)?.summary || ''}</button>) : <p className="muted">{t('empty')}</p>}</div></div>;
}

function MemoryInspector({ memory, entries, nodes, onSelectNode, onOpenDetail, t }: { memory: GraphMemory; entries: GraphPayload['snapshot']['entries']; nodes: GraphNode[]; onSelectNode: (id: number) => void; onOpenDetail: (id: number) => Promise<void>; t: (key: string) => string }) {
  return <div className="detail-stack"><h3>#{memory.memory_id}</h3><p>{memory.summary || memory.content || t('empty')}</p><dl><div><dt>{t('importance')}</dt><dd>{displayImportance(memory.importance).toFixed(1)}</dd></div><div><dt>{t('session')}</dt><dd>{memory.session_id ?? '—'}</dd></div><div><dt>{t('persona')}</dt><dd>{memory.persona_id ?? '—'}</dd></div><div><dt>{t('nodes')}</dt><dd>{memory.node_count ?? nodes.length}</dd></div><div><dt>{t('edges')}</dt><dd>{memory.edge_count ?? 0}</dd></div><div><dt>{t('entries')}</dt><dd>{memory.entry_count ?? entries.length}</dd></div></dl>{nodes.length ? <div className="tag-list">{nodes.map((node) => <button type="button" key={node.id} onClick={() => onSelectNode(node.id)}>{node.label || `#${node.id}`}</button>)}</div> : null}<h3>{t('entries')}</h3>{entries.length ? <ul className="context-entry-list">{entries.map((entry) => <li key={entry.id}><strong>{entry.entry_type ?? t('entry')}</strong><span>{entry.content || '—'}</span></li>)}</ul> : <p className="muted">{t('empty')}</p>}<button className="wf-button wf-button--primary" type="button" onClick={() => void onOpenDetail(memory.memory_id)}>{t('openFullDetail')}</button></div>;
}

function GraphSummaryView({ data, onSelectNode, onSelectMemory, t }: { data: GraphPayload; onSelectNode: (id: number) => void; onSelectMemory: (id: number) => void; t: (key: string) => string }) {
  return <div className="detail-stack"><dl><div><dt>{t('nodes')}</dt><dd>{data.summary.visible_node_count} / {data.summary.graph_node_count}</dd></div><div><dt>{t('edges')}</dt><dd>{data.summary.visible_edge_count} / {data.summary.graph_edge_count}</dd></div><div><dt>{t('entries')}</dt><dd>{data.summary.visible_entry_count} / {data.summary.graph_entry_count}</dd></div><div><dt>{t('retrievalItems')}</dt><dd>{data.retrieval.total}</dd></div></dl><h3>{t('topNodes')}</h3><div className="graph-linked-list">{data.top_nodes.length ? data.top_nodes.map((node) => <button className="wf-button" type="button" key={node.id} onClick={() => onSelectNode(node.id)}>{node.label || `#${node.id}`}</button>) : <p className="muted">{t('empty')}</p>}</div><h3>{t('topMemories')}</h3><div className="graph-linked-list">{data.top_memories.length ? data.top_memories.map((memory) => <button className="wf-button" type="button" key={memory.memory_id} onClick={() => onSelectMemory(memory.memory_id)}>#{memory.memory_id} {memory.summary || ''}</button>) : <p className="muted">{t('empty')}</p>}</div><h3>{t('retrievalItems')}</h3>{data.retrieval.items.length ? <ul className="retrieval-list">{data.retrieval.items.map((item) => <li key={item.memory_id}><button type="button" onClick={() => onSelectMemory(item.memory_id)}>#{item.memory_id}</button><span>{item.content || '—'}</span><strong>{Number(item.final_score ?? 0).toFixed(4)}</strong></li>)}</ul> : <p className="muted">{t('empty')}</p>}<h3>{t('entries')}</h3>{data.snapshot.entries.length ? <ul className="context-entry-list">{data.snapshot.entries.slice(0, 8).map((entry) => <li key={entry.id}><strong>{entry.entry_type ?? t('entry')}</strong><span>{entry.content || '—'}</span></li>)}</ul> : <p className="muted">{t('empty')}</p>}</div>;
}
