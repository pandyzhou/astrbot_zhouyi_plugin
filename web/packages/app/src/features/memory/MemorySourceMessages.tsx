import { formatMemoryTime } from './MemoryDetailDrawer';
import type { MemorySourceMessage } from './types';

export function MemorySourceMessages({ sources }: { sources: MemorySourceMessage[] }) {
  if (!sources.length) return <p className="muted">暂无来源消息</p>;
  return <div className="source-message-list">{sources.map((source) => <article key={source.source_id}><header><strong>{source.source_type}</strong><span className={`status-chip status-chip--${source.availability}`}>{source.availability}</span></header><dl><div><dt>Revision</dt><dd>{source.revision_no}</dd></div><div><dt>会话</dt><dd>{source.session_id ?? '—'}</dd></div><div><dt>消息范围</dt><dd>{source.message_id_start ?? '—'} — {source.message_id_end ?? '—'}</dd></div><div><dt>时间</dt><dd>{formatMemoryTime(source.created_at)}</dd></div></dl>{source.content_snapshot ? <blockquote>{source.content_snapshot}</blockquote> : null}</article>)}</div>;
}
