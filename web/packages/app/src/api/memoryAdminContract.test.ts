import assert from 'node:assert/strict';
import test from 'node:test';
import { MEMORY_ADMIN_PATHS } from './memoryAdminContract';
import { mockRequest } from './mockClient';
import type { ApiEnvelope, MemoryIdentitiesData, MemoryOwnerMergePreviewData } from './types';

const endpoint = (path: string) => `/page/v1/memory/${path}`;

async function request<T>(
  path: string,
  method: 'GET' | 'POST',
  query?: Record<string, string | number | boolean | undefined>,
  body?: unknown,
) {
  return mockRequest<T>(endpoint(path), method, query, body);
}

function expectFailure<T>(result: ApiEnvelope<T>, code: string) {
  if (result.success) assert.fail(`期望失败：${code}`);
  assert.equal(result.error.code, code);
}

function expectSuccess<T>(result: ApiEnvelope<T>): T {
  if (!result.success) assert.fail(result.error.message);
  return result.data;
}

test('Memory Admin identity mutation 路径包含 identities 分段', () => {
  assert.equal(MEMORY_ADMIN_PATHS.ownerCreate, 'identities/owners/create');
  assert.equal(MEMORY_ADMIN_PATHS.ownerUpdate, 'identities/owners/update');
  assert.equal(MEMORY_ADMIN_PATHS.aliasLink, 'identities/aliases/link');
  assert.equal(MEMORY_ADMIN_PATHS.aliasMove, 'identities/aliases/move');
  assert.equal(MEMORY_ADMIN_PATHS.ownerMergePreview, 'identities/owners/merge/preview');
  assert.equal(MEMORY_ADMIN_PATHS.ownerMerge, 'identities/owners/merge');
});

test('对象列表拒绝无 owner，并严格限制为单一 owner', async () => {
  expectFailure(await request(MEMORY_ADMIN_PATHS.objects, 'GET', { page: 1, page_size: 20 }), 'MEMORY_INVALID_REQUEST');

  const result = expectSuccess(await request<{ items: Array<{ owner_user_id: string }> }>(
    MEMORY_ADMIN_PATHS.objects,
    'GET',
    { owner_user_id: 'owner-zhouyi', page: 1, page_size: 20 },
  ));
  assert.ok(result.items.length > 0);
  assert.ok(result.items.every((item) => item.owner_user_id === 'owner-zhouyi'));
});

test('详情、revisions、sources 使用 owner 防止跨 owner 读取', async () => {
  expectFailure(await request(
    MEMORY_ADMIN_PATHS.objectDetail,
    'GET',
    { owner_user_id: 'owner-xingyao', memory_item_id: 'mem-1' },
  ), 'MEMORY_OBJECT_NOT_FOUND');
  expectFailure(await request(
    MEMORY_ADMIN_PATHS.objectRevisions,
    'GET',
    { memory_item_id: 'mem-1' },
  ), 'MEMORY_INVALID_REQUEST');
  expectFailure(await request(
    MEMORY_ADMIN_PATHS.objectSources,
    'GET',
    { memory_item_id: 'mem-1' },
  ), 'MEMORY_INVALID_REQUEST');
});

test('create/update/batch/index retry 强制 expected_version 与 owner', async () => {
  expectFailure(await request(MEMORY_ADMIN_PATHS.objectCreate, 'POST', undefined, {
    owner_user_id: 'owner-zhouyi',
    content: '缺少创建版本',
    scope: 'user',
    memory_type: 'FACT',
  }), 'MEMORY_INVALID_REQUEST');

  expectFailure(await request(MEMORY_ADMIN_PATHS.objectUpdate, 'POST', undefined, {
    owner_user_id: 'owner-xingyao',
    memory_item_id: 'mem-1',
    expected_version: 2,
    content: '跨 owner 更新',
  }), 'MEMORY_OBJECT_NOT_FOUND');

  expectFailure(await request(MEMORY_ADMIN_PATHS.objectBatch, 'POST', undefined, {
    action: 'archive',
    items: [{ memory_item_id: 'mem-1', expected_version: 2 }],
  }), 'MEMORY_INVALID_REQUEST');

  expectFailure(await request(MEMORY_ADMIN_PATHS.indexRetry, 'POST', undefined, {
    owner_user_id: 'owner-zhouyi',
    items: [{ memory_item_id: 'mem-1', expected_version: 999 }],
  }), 'MEMORY_REVISION_CONFLICT');

  expectSuccess(await request(MEMORY_ADMIN_PATHS.indexRetry, 'POST', undefined, {
    owner_user_id: 'owner-zhouyi',
    items: [{ memory_item_id: 'mem-1', expected_version: 2 }],
  }));
});

test('更新兼容真实后端的 canonical_key 字符串约束', async () => {
  expectFailure(await request(MEMORY_ADMIN_PATHS.objectUpdate, 'POST', undefined, {
    owner_user_id: 'owner-zhouyi',
    memory_item_id: 'mem-1',
    expected_version: 2,
    content: '尝试发送空 canonical_key',
    canonical_key: null,
  }), 'MEMORY_INVALID_REQUEST');
});

test('对象 merge 执行复用 preview 的版本快照与结构化 payload', async () => {
  const left = expectSuccess(await request<{ item: { memory_item_id: string; version: number } }>(
    MEMORY_ADMIN_PATHS.objectCreate,
    'POST',
    undefined,
    {
      owner_user_id: 'owner-zhouyi',
      expected_version: 0,
      scope: 'user',
      memory_type: 'FACT',
      content: '左侧内容',
      structured_payload: { left: true },
    },
  ));
  const right = expectSuccess(await request<{ item: { memory_item_id: string; version: number } }>(
    MEMORY_ADMIN_PATHS.objectCreate,
    'POST',
    undefined,
    {
      owner_user_id: 'owner-zhouyi',
      expected_version: 0,
      scope: 'user',
      memory_type: 'FACT',
      content: '右侧内容',
      structured_payload: { right: true },
    },
  ));
  const expectedVersions = {
    [left.item.memory_item_id]: left.item.version,
    [right.item.memory_item_id]: right.item.version,
  };
  const preview = expectSuccess(await request<{
    owner_user_id: string;
    survivor_memory_item_id: string;
    source_memory_item_ids: string[];
    expected_versions: Record<string, number>;
    merged_content: string;
    merged_structured_payload: Record<string, unknown>;
  }>(MEMORY_ADMIN_PATHS.objectMergePreview, 'POST', undefined, {
    owner_user_id: 'owner-zhouyi',
    survivor_memory_item_id: left.item.memory_item_id,
    source_memory_item_ids: [right.item.memory_item_id],
    expected_versions: expectedVersions,
  }));

  const merged = expectSuccess(await request<{
    item: { structured_payload: Record<string, unknown> };
  }>(MEMORY_ADMIN_PATHS.objectMerge, 'POST', undefined, {
    owner_user_id: preview.owner_user_id,
    survivor_memory_item_id: preview.survivor_memory_item_id,
    source_memory_item_ids: preview.source_memory_item_ids,
    expected_versions: preview.expected_versions,
    content: preview.merged_content,
    structured_payload: preview.merged_structured_payload,
  }));
  assert.deepEqual(merged.item.structured_payload, { left: true, right: true });
});

test('merge、supersede 与 conflict 都要求 owner 和完整 expected_versions', async () => {
  expectFailure(await request(MEMORY_ADMIN_PATHS.objectMergePreview, 'POST', undefined, {
    survivor_memory_item_id: 'mem-2',
    source_memory_item_ids: ['mem-2-alt'],
    expected_versions: { 'mem-2': 1, 'mem-2-alt': 2 },
  }), 'MEMORY_INVALID_REQUEST');

  expectFailure(await request(MEMORY_ADMIN_PATHS.objectSupersede, 'POST', undefined, {
    owner_user_id: 'owner-zhouyi',
    old_memory_item_id: 'mem-2',
    new_memory_item_id: 'mem-2-alt',
    expected_versions: { 'mem-2': 1 },
  }), 'MEMORY_INVALID_REQUEST');

  expectFailure(await request(MEMORY_ADMIN_PATHS.conflicts, 'GET'), 'MEMORY_INVALID_REQUEST');
  expectFailure(await request(MEMORY_ADMIN_PATHS.conflictResolve, 'POST', undefined, {
    conflict_id: 'conflict-1',
    action: 'dismiss',
    expected_versions: { 'mem-2': 1, 'mem-2-alt': 2 },
  }), 'MEMORY_INVALID_REQUEST');
});

test('旧 identity 短路径不再被 mock 接受', async () => {
  expectFailure(await request('owners/create', 'POST', undefined, { display_name: '错误路径' }), 'ENDPOINT_NOT_FOUND');
  expectFailure(await request('aliases/move', 'POST', undefined, {
    identity_link_id: '1',
    owner_user_id: 'owner-xingyao',
    expected_owner_user_id: 'owner-zhouyi',
  }), 'ENDPOINT_NOT_FOUND');
});

test('owner update 与 alias move 强制 expected state', async () => {
  const identities = expectSuccess(await request<MemoryIdentitiesData>(
    MEMORY_ADMIN_PATHS.identities,
    'GET',
  ));
  const zhouyi = identities.owners.find((owner) => owner.owner_user_id === 'owner-zhouyi');
  assert.ok(zhouyi);

  expectFailure(await request(MEMORY_ADMIN_PATHS.ownerUpdate, 'POST', undefined, {
    owner_user_id: zhouyi.owner_user_id,
    display_name: zhouyi.display_name,
    status: zhouyi.status,
  }), 'MEMORY_INVALID_REQUEST');

  expectSuccess(await request(MEMORY_ADMIN_PATHS.ownerUpdate, 'POST', undefined, {
    owner_user_id: zhouyi.owner_user_id,
    display_name: zhouyi.display_name,
    status: zhouyi.status,
    expected_updated_at: zhouyi.expected_updated_at,
  }));

  expectFailure(await request(MEMORY_ADMIN_PATHS.aliasMove, 'POST', undefined, {
    identity_link_id: '1',
    owner_user_id: 'owner-xingyao',
  }), 'MEMORY_INVALID_REQUEST');

  expectSuccess(await request(MEMORY_ADMIN_PATHS.aliasMove, 'POST', undefined, {
    identity_link_id: '1',
    owner_user_id: 'owner-xingyao',
    expected_owner_user_id: 'owner-zhouyi',
  }));
});

test('owner merge 必须使用 preview_id 与 expected_owner_states 两阶段执行', async () => {
  const preview = expectSuccess(await request<MemoryOwnerMergePreviewData>(
    MEMORY_ADMIN_PATHS.ownerMergePreview,
    'POST',
    undefined,
    {
      survivor_owner_user_id: 'owner-zhouyi',
      source_owner_user_ids: ['owner-xingyao'],
    },
  ));
  assert.ok(preview.preview_id);
  assert.ok(preview.expected_owner_states['owner-zhouyi']);
  assert.ok(preview.expected_owner_states['owner-xingyao']);

  expectFailure(await request(MEMORY_ADMIN_PATHS.ownerMerge, 'POST', undefined, {
    survivor_owner_user_id: preview.survivor_owner_user_id,
    source_owner_user_ids: preview.source_owner_user_ids,
  }), 'MEMORY_INVALID_REQUEST');

  expectSuccess(await request(MEMORY_ADMIN_PATHS.ownerMerge, 'POST', undefined, {
    survivor_owner_user_id: preview.survivor_owner_user_id,
    source_owner_user_ids: preview.source_owner_user_ids,
    preview_id: preview.preview_id,
    expected_owner_states: preview.expected_owner_states,
  }));
});
