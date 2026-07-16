import type {
  MemoryIdentityAlias,
  MemoryMergeInput,
  MemoryMergePreviewData,
  MemoryObject,
  MemoryObjectMutationInput,
  MemoryObjectScope,
  MemoryObjectUpdateInput,
} from './types';
import {
  MEMORY_CONFLICTS_QUERY_PREFIX,
  MEMORY_IDENTITIES_QUERY_PREFIX,
  MEMORY_MAINTENANCE_QUERY_PREFIX,
  MEMORY_OBJECTS_QUERY_PREFIX,
  MEMORY_OVERVIEW_QUERY_PREFIX,
} from '../../store/queryKeys';

export interface MemoryObjectDraft {
  owner_user_id: string;
  scope: MemoryObjectScope;
  persona_id: string;
  session_id: string;
  memory_type: string;
  canonical_key: string;
  content: string;
  importance: string;
  confidence: string;
  group_safe: boolean;
  reason: string;
}

export interface RevisionConflictState {
  draft: MemoryObjectDraft;
  baselineVersion: number;
  latest: MemoryObject;
}

export const memoryAdminInvalidationPrefixes = [
  MEMORY_OBJECTS_QUERY_PREFIX,
  MEMORY_CONFLICTS_QUERY_PREFIX,
  MEMORY_IDENTITIES_QUERY_PREFIX,
  MEMORY_MAINTENANCE_QUERY_PREFIX,
  MEMORY_OVERVIEW_QUERY_PREFIX,
] as const;

export function draftFromObject(item?: MemoryObject | null): MemoryObjectDraft {
  return {
    owner_user_id: item?.owner_user_id ?? '',
    scope: item?.scope ?? 'persona',
    persona_id: item?.persona_id ?? '',
    session_id: item?.session_id ?? '',
    memory_type: item?.memory_type ?? 'GENERAL',
    canonical_key: item?.canonical_key ?? '',
    content: item?.content ?? '',
    importance: String(item?.importance ?? 0.5),
    confidence: String(item?.confidence ?? 0.5),
    group_safe: item?.group_safe ?? false,
    reason: '',
  };
}

export function validateMemoryScope(
  draft: MemoryObjectDraft,
  item?: MemoryObject | null,
): string | null {
  if (!draft.owner_user_id.trim()) return 'owner_user_id 必填';
  if (!draft.content.trim()) return '内容不能为空';
  if (draft.scope === 'persona' && !draft.persona_id.trim()) return 'persona scope 必须填写 persona_id';
  if ((draft.scope === 'session' || draft.scope === 'legacy_session') && !draft.session_id.trim()) return `${draft.scope} scope 必须填写 session_id`;
  if (item?.canonical_key && !draft.canonical_key.trim()) {
    return '当前后端不支持清空 canonical_key；请保留原值或填写新值';
  }
  return null;
}

export function createPayloadFromDraft(draft: MemoryObjectDraft): MemoryObjectMutationInput {
  return {
    owner_user_id: draft.owner_user_id.trim(),
    expected_version: 0,
    scope: draft.scope,
    content: draft.content.trim(),
    persona_id: draft.persona_id.trim() || null,
    session_id: draft.session_id.trim() || null,
    memory_type: draft.memory_type.trim() || 'GENERAL',
    canonical_key: draft.canonical_key.trim() || null,
    importance: Number(draft.importance),
    confidence: Number(draft.confidence),
    group_safe: draft.group_safe,
    reason: draft.reason.trim() || undefined,
  };
}

export function updatePayloadFromDraft(item: MemoryObject, draft: MemoryObjectDraft): MemoryObjectUpdateInput {
  const {
    canonical_key: canonicalKey,
    expected_version: _createExpectedVersion,
    ...payload
  } = createPayloadFromDraft(draft);
  return {
    ...payload,
    ...(canonicalKey ? { canonical_key: canonicalKey } : {}),
    memory_item_id: item.memory_item_id,
    expected_version: item.version,
  };
}

export function buildMergePayload(
  preview: MemoryMergePreviewData,
  content: string,
  reason = '',
): MemoryMergeInput {
  const mergedContent = content.trim();
  if (!mergedContent) throw new Error('合并后内容不能为空');
  if (!preview.owner_user_id.trim()) throw new Error('合并预览缺少 owner_user_id');
  if (!preview.survivor_memory_item_id.trim()) throw new Error('合并预览缺少保留对象');
  if (!preview.source_memory_item_ids.length) throw new Error('合并预览至少需要一个来源对象');

  const selectedIds = [preview.survivor_memory_item_id, ...preview.source_memory_item_ids];
  if (selectedIds.some((itemId) => !Number.isInteger(preview.expected_versions[itemId]))) {
    throw new Error('合并预览缺少完整 expected_versions');
  }

  return {
    owner_user_id: preview.owner_user_id,
    survivor_memory_item_id: preview.survivor_memory_item_id,
    source_memory_item_ids: [...preview.source_memory_item_ids],
    expected_versions: { ...preview.expected_versions },
    content: mergedContent,
    structured_payload: preview.merged_structured_payload,
    reason: reason.trim() || undefined,
  };
}

export function preserveDraftOnRevisionConflict(
  draft: MemoryObjectDraft,
  baselineVersion: number,
  latest: MemoryObject,
): RevisionConflictState {
  return { draft: { ...draft }, baselineVersion, latest };
}

export function aliasesByOwner(aliases: MemoryIdentityAlias[]): Map<string, MemoryIdentityAlias[]> {
  const result = new Map<string, MemoryIdentityAlias[]>();
  aliases.forEach((alias) => {
    const bucket = result.get(alias.owner_user_id) ?? [];
    bucket.push(alias);
    result.set(alias.owner_user_id, bucket);
  });
  return result;
}
