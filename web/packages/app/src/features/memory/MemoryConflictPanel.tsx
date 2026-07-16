import { useState } from 'react';
import { DataState } from '@pandyzhou/astrbot-mc-ui';
import { memoryAdminClient } from '../../api/client';
import { queryCache } from '../../store/queryCacheCore';
import {
  MEMORY_CONFLICTS_QUERY_PREFIX,
  MEMORY_OBJECTS_QUERY_PREFIX,
  queryKeys,
} from '../../store/queryKeys';
import { useCachedQuery } from '../../store/useCachedQuery';
import type { MemoryConflict, MemoryOwner } from './types';

interface Props {
  owners: MemoryOwner[];
  ownerUserId: string;
  onOwnerChange: (ownerUserId: string) => void;
}

export function MemoryConflictPanel({ owners, ownerUserId, onOwnerChange }: Props) {
  const query = useCachedQuery<MemoryConflict[]>(
    queryKeys.memoryConflicts(ownerUserId),
    () => memoryAdminClient.conflicts(ownerUserId),
    { enabled: Boolean(ownerUserId) },
  );
  const [busy, setBusy] = useState('');
  const [feedback, setFeedback] = useState('');

  const resolve = async (
    conflict: MemoryConflict,
    action: 'merge' | 'supersede_left' | 'supersede_right' | 'dismiss',
  ) => {
    setBusy(conflict.conflict_id);
    setFeedback('');
    try {
      await memoryAdminClient.resolveConflict({
        owner_user_id: conflict.owner_user_id || ownerUserId,
        conflict_id: conflict.conflict_id,
        action,
        survivor_memory_item_id: action === 'merge'
          ? conflict.left_item.memory_item_id
          : undefined,
        expected_versions: {
          [conflict.left_item.memory_item_id]: conflict.left_item.version,
          [conflict.right_item.memory_item_id]: conflict.right_item.version,
        },
      });
      queryCache.invalidate(MEMORY_CONFLICTS_QUERY_PREFIX);
      queryCache.invalidate(MEMORY_OBJECTS_QUERY_PREFIX);
      await query.refresh();
      setFeedback('冲突已处理');
    } catch (reason) {
      setFeedback(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy('');
    }
  };

  return (
    <div className="memory-conflict-workspace">
      <label className="wf-label">
        Owner
        <select
          className="wf-input"
          value={ownerUserId}
          onChange={(event) => onOwnerChange(event.target.value)}
          required
        >
          <option value="" disabled>全部 owner（请选择具体 owner）</option>
          {owners.map((owner) => (
            <option key={owner.owner_user_id} value={owner.owner_user_id}>
              {owner.display_name}
            </option>
          ))}
        </select>
      </label>

      {feedback ? <p className="inline-feedback" role="status">{feedback}</p> : null}

      {!ownerUserId ? (
        <DataState
          state="empty"
          title="请选择具体 owner"
          message="冲突列表不会执行跨 owner 查询"
        />
      ) : query.isInitialLoading ? (
        <DataState state="loading" title="正在加载" message="冲突列表" />
      ) : !query.data && query.error ? (
        <DataState
          state="error"
          title="加载失败"
          message={query.error instanceof Error ? query.error.message : String(query.error)}
        />
      ) : query.data?.length ? (
        query.data.map((conflict) => (
          <article className="memory-conflict-card" key={conflict.conflict_id}>
            <header>
              <div>
                <strong>{conflict.conflict_type}</strong>
                <span className={`status-chip status-chip--${conflict.severity}`}>
                  {conflict.severity}
                </span>
              </div>
              <code>{conflict.conflict_id}</code>
            </header>
            <div className="memory-conflict-compare">
              <section>
                <h3>左侧 · {conflict.left_item.memory_item_id}</h3>
                <p>{conflict.left_item.content}</p>
                <small>
                  v{conflict.left_item.version} ·{' '}
                  {conflict.left_item.owner_display_name ?? conflict.left_item.owner_user_id}
                </small>
              </section>
              <section>
                <h3>右侧 · {conflict.right_item.memory_item_id}</h3>
                <p>{conflict.right_item.content}</p>
                <small>
                  v{conflict.right_item.version} ·{' '}
                  {conflict.right_item.owner_display_name ?? conflict.right_item.owner_user_id}
                </small>
              </section>
            </div>
            <footer>
              <button
                className="wf-button"
                type="button"
                disabled={busy === conflict.conflict_id}
                onClick={() => void resolve(conflict, 'merge')}
              >
                合并
              </button>
              <button
                className="wf-button"
                type="button"
                disabled={busy === conflict.conflict_id}
                onClick={() => void resolve(conflict, 'supersede_right')}
              >
                左侧取代右侧
              </button>
              <button
                className="wf-button"
                type="button"
                disabled={busy === conflict.conflict_id}
                onClick={() => void resolve(conflict, 'supersede_left')}
              >
                右侧取代左侧
              </button>
              <button
                className="wf-button"
                type="button"
                disabled={busy === conflict.conflict_id}
                onClick={() => void resolve(conflict, 'dismiss')}
              >
                忽略
              </button>
            </footer>
          </article>
        ))
      ) : (
        <DataState state="empty" title="没有待处理冲突" message="所有对象冲突均已解决" />
      )}
    </div>
  );
}
