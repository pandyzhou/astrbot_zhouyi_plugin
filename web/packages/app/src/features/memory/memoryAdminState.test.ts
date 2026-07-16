import assert from 'node:assert/strict';
import test from 'node:test';
import type { MemoryIdentityAlias, MemoryMergePreviewData, MemoryObject } from './types';
import {
  aliasesByOwner,
  buildMergePayload,
  createPayloadFromDraft,
  draftFromObject,
  memoryAdminInvalidationPrefixes,
  preserveDraftOnRevisionConflict,
  updatePayloadFromDraft,
  validateMemoryScope,
} from './memoryAdminState';

const item = (id: string, version: number): MemoryObject => ({
  memory_item_id: id,
  owner_user_id: 'owner-1',
  owner_display_name: '测试用户',
  scope: 'persona',
  session_id: null,
  persona_id: 'persona-main',
  memory_type: 'FACT',
  canonical_key: null,
  status: 'active',
  content: `内容 ${id}`,
  structured_payload: null,
  current_revision_no: version,
  version,
  importance: 0.8,
  confidence: 0.9,
  useful_score: 2,
  group_safe: false,
  current_document_id: null,
  index_status: 'synced',
  conflict_count: 0,
  source_count: 1,
  relation_count: 0,
  created_at: 1,
  updated_at: 2,
});

const mergePreview = (): MemoryMergePreviewData => ({
  owner_user_id: 'owner-1',
  survivor_memory_item_id: 'a',
  source_memory_item_ids: ['b'],
  merged_content: '预览内容',
  merged_structured_payload: { merged: true },
  warnings: [],
  expected_versions: { a: 2, b: 5 },
});

test('scope 校验要求 persona/session 标识', () => {
  const draft = draftFromObject(item('a', 2));
  draft.persona_id = '';
  assert.match(validateMemoryScope(draft) ?? '', /persona_id/);
  draft.scope = 'session';
  draft.session_id = '';
  assert.match(validateMemoryScope(draft) ?? '', /session_id/);
});

test('创建 payload 固定 expected_version=0 并携带 owner', () => {
  const draft = draftFromObject(item('a', 7));
  const payload = createPayloadFromDraft(draft);
  assert.equal(payload.owner_user_id, 'owner-1');
  assert.equal(payload.expected_version, 0);
});

test('更新 payload 固定 owner 与 expected_version', () => {
  const current = item('a', 7);
  const payload = updatePayloadFromDraft(current, draftFromObject(current));
  assert.equal(payload.owner_user_id, 'owner-1');
  assert.equal(payload.expected_version, 7);
  assert.equal(payload.memory_item_id, 'a');
});

test('更新时不发送后端不接受的空 canonical_key 字符串', () => {
  const current = item('a', 7);
  const draft = draftFromObject(current);
  draft.canonical_key = '';
  const payload = updatePayloadFromDraft(current, draft);
  assert.equal('canonical_key' in payload, false);

  const keyed = { ...current, canonical_key: 'stable-key' };
  assert.match(validateMemoryScope({ ...draftFromObject(keyed), canonical_key: '' }, keyed) ?? '', /不支持清空/);
});

test('merge payload 绑定 preview 的 owner、对象版本与结构化内容', () => {
  const preview = mergePreview();
  const payload = buildMergePayload(preview, '合并内容');
  assert.equal(payload.owner_user_id, 'owner-1');
  assert.equal(payload.survivor_memory_item_id, 'a');
  assert.deepEqual(payload.source_memory_item_ids, ['b']);
  assert.deepEqual(payload.expected_versions, { a: 2, b: 5 });
  assert.deepEqual(payload.structured_payload, { merged: true });

  assert.throws(() => buildMergePayload({
    ...preview,
    expected_versions: { a: 2 },
  }, '内容'), /expected_versions/);
  assert.throws(() => buildMergePayload(preview, '   '), /内容不能为空/);
});

test('409 状态保留草稿且记录最新对象', () => {
  const original = item('a', 2);
  const draft = draftFromObject(original);
  draft.content = '尚未提交的草稿';
  const conflict = preserveDraftOnRevisionConflict(draft, 2, item('a', 3));
  assert.equal(conflict.draft.content, '尚未提交的草稿');
  assert.equal(conflict.baselineVersion, 2);
  assert.equal(conflict.latest.version, 3);
});

test('缓存失效覆盖对象、冲突、身份、维护和概览', () => {
  assert.equal(memoryAdminInvalidationPrefixes.length, 5);
  assert.deepEqual(memoryAdminInvalidationPrefixes[0], ['memory', 'objects']);
});

test('identity alias 按 owner O(1) 分组', () => {
  const aliases: MemoryIdentityAlias[] = [
    { identity_link_id: '1', owner_user_id: 'a', platform_id: 'qq', bot_id: 'bot', external_user_id: '1', verified: true, source: 'manual', status: 'active', created_at: null, updated_at: null },
    { identity_link_id: '2', owner_user_id: 'a', platform_id: 'telegram', bot_id: 'bot', external_user_id: '2', verified: true, source: 'manual', status: 'active', created_at: null, updated_at: null },
  ];
  assert.equal(aliasesByOwner(aliases).get('a')?.length, 2);
});
