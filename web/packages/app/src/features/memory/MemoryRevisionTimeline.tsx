import { formatMemoryTime } from './MemoryDetailDrawer';
import type { MemoryRevision } from './types';

export function MemoryRevisionTimeline({ revisions, selected, onSelect }: { revisions: MemoryRevision[]; selected?: number; onSelect?: (revision: MemoryRevision) => void }) {
  if (!revisions.length) return <p className="muted">暂无 revision</p>;
  return <ol className="revision-timeline">{[...revisions].sort((a, b) => b.revision_no - a.revision_no).map((revision) => <li key={revision.revision_no} className={selected === revision.revision_no ? 'is-selected' : ''}><button type="button" onClick={() => onSelect?.(revision)}><span><strong>Revision {revision.revision_no}</strong><span className="type-chip">{revision.operation}</span></span><time>{formatMemoryTime(revision.created_at)}</time><p>{revision.content}</p>{revision.reason ? <small>{revision.reason}</small> : null}</button></li>)}</ol>;
}
