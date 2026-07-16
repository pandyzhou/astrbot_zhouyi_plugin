import { useEffect, useMemo, useState } from 'react';
import { DataState } from '@pandyzhou/astrbot-mc-ui';
import { memoryAdminClient } from '../../api/client';
import type {
  MemoryIdentityAlias,
  MemoryOwner,
  MemoryOwnerMergePreviewData,
  MemoryOwnerStatus,
} from '../../api/types';
import { queryCache } from '../../store/queryCacheCore';
import {
  MEMORY_IDENTITIES_QUERY_PREFIX,
  MEMORY_OBJECTS_QUERY_PREFIX,
  queryKeys,
} from '../../store/queryKeys';
import { useCachedQuery } from '../../store/useCachedQuery';

export function IdentityMappingsPanel() {
  const query = useCachedQuery(
    queryKeys.memoryIdentities,
    () => memoryAdminClient.identities(),
  );
  const [ownerName, setOwnerName] = useState('');
  const [alias, setAlias] = useState({
    owner_user_id: '',
    platform_id: 'qq',
    bot_id: '',
    external_user_id: '',
  });
  const [merge, setMerge] = useState({ survivor: '', sources: '' });
  const [mergePreview, setMergePreview] = useState<MemoryOwnerMergePreviewData | null>(null);
  const [feedback, setFeedback] = useState('');
  const [busy, setBusy] = useState(false);
  const ownerById = useMemo(
    () => new Map((query.data?.owners ?? []).map((owner) => [owner.owner_user_id, owner])),
    [query.data],
  );

  const refresh = async () => {
    queryCache.invalidate(MEMORY_IDENTITIES_QUERY_PREFIX);
    queryCache.invalidate(MEMORY_OBJECTS_QUERY_PREFIX);
    await query.refresh();
  };

  const run = async (
    action: () => Promise<unknown>,
    message = '身份映射已更新',
  ): Promise<boolean> => {
    setBusy(true);
    setFeedback('');
    try {
      await action();
      await refresh();
      setFeedback(message);
      return true;
    } catch (reason) {
      setFeedback(reason instanceof Error ? reason.message : String(reason));
      return false;
    } finally {
      setBusy(false);
    }
  };

  const sourceOwnerIds = () => merge.sources
    .split(',')
    .map((value) => value.trim())
    .filter(Boolean);

  const previewOwnerMerge = async () => {
    setBusy(true);
    setFeedback('');
    setMergePreview(null);
    try {
      setMergePreview(await memoryAdminClient.ownerMergePreview(
        merge.survivor,
        sourceOwnerIds(),
      ));
    } catch (reason) {
      setFeedback(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  };

  if (query.isInitialLoading) {
    return <DataState state="loading" title="正在加载" message="身份映射" />;
  }
  if (!query.data && query.error) {
    return (
      <DataState
        state="error"
        title="加载失败"
        message={query.error instanceof Error ? query.error.message : String(query.error)}
      />
    );
  }

  const owners = query.data?.owners ?? [];

  return (
    <div className="identity-workspace">
      {feedback ? <p className="inline-feedback" role="status">{feedback}</p> : null}

      <section className="identity-actions">
        <form onSubmit={(event) => {
          event.preventDefault();
          void run(() => memoryAdminClient.createOwner(ownerName)).then((succeeded) => {
            if (succeeded) setOwnerName('');
          });
        }}>
          <h3>新建 owner</h3>
          <label className="wf-label">
            显示名
            <input
              className="wf-input"
              value={ownerName}
              onChange={(event) => setOwnerName(event.target.value)}
              required
            />
          </label>
          <button className="wf-button wf-button--primary" disabled={busy}>新建</button>
        </form>

        <form onSubmit={(event) => {
          event.preventDefault();
          void run(() => memoryAdminClient.linkAlias(alias));
        }}>
          <h3>绑定 alias</h3>
          <label className="wf-label">
            Owner
            <select
              className="wf-input"
              value={alias.owner_user_id}
              onChange={(event) => setAlias((value) => ({
                ...value,
                owner_user_id: event.target.value,
              }))}
              required
            >
              <option value="">选择 owner</option>
              {owners.map((owner) => (
                <option key={owner.owner_user_id} value={owner.owner_user_id}>
                  {owner.display_name}
                </option>
              ))}
            </select>
          </label>
          <label className="wf-label">
            平台
            <input
              className="wf-input"
              value={alias.platform_id}
              onChange={(event) => setAlias((value) => ({
                ...value,
                platform_id: event.target.value,
              }))}
            />
          </label>
          <label className="wf-label">
            Bot ID
            <input
              className="wf-input"
              value={alias.bot_id}
              onChange={(event) => setAlias((value) => ({
                ...value,
                bot_id: event.target.value,
              }))}
              required
            />
          </label>
          <label className="wf-label">
            外部用户 ID
            <input
              className="wf-input"
              value={alias.external_user_id}
              onChange={(event) => setAlias((value) => ({
                ...value,
                external_user_id: event.target.value,
              }))}
              required
            />
          </label>
          <button className="wf-button wf-button--primary" disabled={busy}>显式绑定</button>
        </form>

        <form onSubmit={(event) => {
          event.preventDefault();
          void previewOwnerMerge();
        }}>
          <h3>合并 owner</h3>
          <label className="wf-label">
            保留 Owner
            <select
              className="wf-input"
              value={merge.survivor}
              onChange={(event) => {
                setMerge((value) => ({ ...value, survivor: event.target.value }));
                setMergePreview(null);
              }}
              required
            >
              <option value="">选择 owner</option>
              {owners.map((owner) => (
                <option key={owner.owner_user_id} value={owner.owner_user_id}>
                  {owner.display_name}
                </option>
              ))}
            </select>
          </label>
          <label className="wf-label">
            来源 Owner ID（逗号分隔）
            <input
              className="wf-input"
              value={merge.sources}
              onChange={(event) => {
                setMerge((value) => ({ ...value, sources: event.target.value }));
                setMergePreview(null);
              }}
              required
            />
          </label>
          <button className="wf-button" disabled={busy}>生成合并预览</button>
          {mergePreview ? (
            <section className="owner-merge-preview" aria-label="Owner 合并预览">
              <dl>
                <div><dt>Alias</dt><dd>{mergePreview.alias_count}</dd></div>
                <div><dt>记忆对象</dt><dd>{mergePreview.memory_item_count}</dd></div>
                <div><dt>冲突</dt><dd>{mergePreview.conflict_count}</dd></div>
              </dl>
              {mergePreview.warnings.length ? (
                <ul>
                  {mergePreview.warnings.map((warning) => <li key={warning}>{warning}</li>)}
                </ul>
              ) : null}
              <button
                className="wf-button wf-button--primary"
                type="button"
                disabled={busy}
                onClick={() => void run(() => memoryAdminClient.mergeOwners({
                  survivor_owner_user_id: mergePreview.survivor_owner_user_id,
                  source_owner_user_ids: mergePreview.source_owner_user_ids,
                  preview_id: mergePreview.preview_id,
                  expected_owner_states: mergePreview.expected_owner_states,
                }), 'Owner 已合并').then((succeeded) => {
                  if (succeeded) setMergePreview(null);
                })}
              >
                确认合并
              </button>
            </section>
          ) : null}
        </form>
      </section>

      <div className="owner-list">
        {owners.map((owner) => (
          <OwnerCard
            key={owner.owner_user_id}
            owner={owner}
            owners={owners}
            busy={busy}
            onUpdate={(displayName, status) => run(() => memoryAdminClient.updateOwner({
              owner_user_id: owner.owner_user_id,
              display_name: displayName,
              status,
              expected_updated_at: owner.expected_updated_at,
            }), 'Owner 已更新')}
            onMoveAlias={(item, targetOwnerUserId) => run(() => memoryAdminClient.moveAlias({
              identity_link_id: item.identity_link_id,
              owner_user_id: targetOwnerUserId,
              expected_owner_user_id: item.owner_user_id,
            }), 'Alias 已移动')}
          />
        ))}
      </div>

      {query.data?.unmapped_aliases.length ? (
        <section className="unmapped-aliases">
          <h3>未解析 alias</h3>
          {query.data.unmapped_aliases.map((item) => (
            <article key={item.identity_link_id}>
              <code>{item.platform_id} / {item.bot_id} / {item.external_user_id}</code>
              {item.owner_user_id ? (
                <select
                  aria-label={`移动 ${item.external_user_id}`}
                  className="wf-input"
                  defaultValue=""
                  onChange={(event) => {
                    if (ownerById.has(event.target.value)) {
                      void run(() => memoryAdminClient.moveAlias({
                        identity_link_id: item.identity_link_id,
                        owner_user_id: event.target.value,
                        expected_owner_user_id: item.owner_user_id,
                      }));
                    }
                  }}
                >
                  <option value="">移动到 owner…</option>
                  {owners.map((owner) => (
                    <option key={owner.owner_user_id} value={owner.owner_user_id}>
                      {owner.display_name}
                    </option>
                  ))}
                </select>
              ) : (
                <small>后端未提供 expected_owner_user_id，禁止无条件移动</small>
              )}
            </article>
          ))}
        </section>
      ) : null}
    </div>
  );
}

interface OwnerCardProps {
  owner: MemoryOwner;
  owners: MemoryOwner[];
  busy: boolean;
  onUpdate: (displayName: string, status: MemoryOwnerStatus) => Promise<unknown> | void;
  onMoveAlias: (alias: MemoryIdentityAlias, targetOwnerUserId: string) => Promise<unknown> | void;
}

function OwnerCard({ owner, owners, busy, onUpdate, onMoveAlias }: OwnerCardProps) {
  const [displayName, setDisplayName] = useState(owner.display_name);
  const [status, setStatus] = useState<MemoryOwnerStatus>(owner.status);

  useEffect(() => {
    setDisplayName(owner.display_name);
    setStatus(owner.status);
  }, [owner.display_name, owner.status]);

  return (
    <article>
      <header>
        <div>
          <strong>{owner.display_name || owner.owner_user_id}</strong>
          <code>{owner.owner_user_id}</code>
        </div>
        <span className={`status-chip status-chip--${owner.status}`}>{owner.status}</span>
      </header>

      <form onSubmit={(event) => {
        event.preventDefault();
        void onUpdate(displayName, status);
      }}>
        <label className="wf-label">
          显示名
          <input
            className="wf-input"
            value={displayName}
            onChange={(event) => setDisplayName(event.target.value)}
            required
          />
        </label>
        <label className="wf-label">
          状态
          <select
            className="wf-input"
            value={status}
            onChange={(event) => setStatus(event.target.value as MemoryOwnerStatus)}
            disabled={owner.status === 'merged'}
          >
            <option value="active">active</option>
            <option value="disabled">disabled</option>
            {owner.status === 'merged' ? <option value="merged">merged</option> : null}
          </select>
        </label>
        <button
          className="wf-button"
          type="submit"
          disabled={busy || owner.status === 'merged' || !owner.expected_updated_at}
        >
          更新 owner
        </button>
      </form>

      <div className="alias-list">
        {owner.aliases.length ? owner.aliases.map((item) => (
          <div key={item.identity_link_id}>
            <span>{item.platform_id}</span>
            <code>{item.bot_id} / {item.external_user_id}</code>
            <small>{item.verified ? '已验证' : '未验证'}</small>
            <select
              aria-label={`移动 ${item.external_user_id}`}
              className="wf-input"
              defaultValue=""
              disabled={busy}
              onChange={(event) => {
                if (event.target.value) void onMoveAlias(item, event.target.value);
              }}
            >
              <option value="">移动到 owner…</option>
              {owners
                .filter((candidate) => candidate.owner_user_id !== owner.owner_user_id)
                .map((candidate) => (
                  <option key={candidate.owner_user_id} value={candidate.owner_user_id}>
                    {candidate.display_name}
                  </option>
                ))}
            </select>
          </div>
        )) : <p className="muted">暂无 alias</p>}
      </div>
    </article>
  );
}
