import { MEMORY_ADMIN_PATHS } from './memoryAdminContract';
import type {
  ApiEnvelope,
  CleanupCandidate,
  GroupRuntimeSettingKey,
  MemoryConfigData,
  MemoryConfigMutationInput,
  MemoryConfigObject,
  MemoryConfigSaveData,
  MemoryConflict,
  MemoryIdentitiesData,
  MemoryMaintenanceStatusData,
  MemoryObject,
  MemoryObjectMutationInput,
  MemoryObjectUpdateInput,
  MemoryRevision,
  MemorySourceMessage,
  RuntimeSettingKey,
  RuntimeSettings,
  SettingsData,
  SettingsMutationInput,
  SettingsPreviewData,
  SettingsSaveData,
  SourceUpdatesData,
} from './types';

type Query = Record<string, string | number | boolean | undefined>;

interface SavedServer {
  id: number;
  name: string;
  host: string;
  created_time: number;
  last_success_time: number | null;
  last_failed_time: number | null;
  failed_count: number;
}

interface RawTrendPoint {
  ts: number;
  count: number;
}

const globalSettings: RuntimeSettings = {
  max_history_points: 168,
  trend_sampling_enabled: true,
  auto_cleanup_enabled: true,
  auto_cleanup_days: 10,
  auto_refresh_on_page_open: true,
  default_trend_hours: 24,
  mc_lookup_timeout_seconds: 5,
  mc_status_timeout_seconds: 8,
  max_concurrent_queries: 4,
};
const groupOverrides: Record<string, Partial<Pick<RuntimeSettings, GroupRuntimeSettingKey>>> = {
  '10001': {},
  '10002': { default_trend_hours: 72, auto_refresh_on_page_open: false },
};
let globalRevision = 1;
const groupRevisions: Record<string, number> = { '10001': 1, '10002': 1 };
const settingsConstraints: SettingsData['constraints'] = {
  max_history_points: { min: 168, max: 100000, step: 1, unit: '点/服务器' },
  auto_cleanup_days: { min: 1, max: 365, step: 1, unit: '天' },
  default_trend_hours: { min: 1, max: 168, step: 1, unit: '小时' },
  mc_lookup_timeout_seconds: { min: 0.5, max: 30, step: 0.5, unit: '秒' },
  mc_status_timeout_seconds: { min: 1, max: 60, step: 0.5, unit: '秒' },
  max_concurrent_queries: { min: 1, max: 20, step: 1, unit: '个' },
};
const settingsPreviews = new Map<string, { signature: string; pointsToDelete: number }>();
let previewSequence = 0;
const now = () => Math.floor(Date.now() / 1000);
const bucketNow = () => Math.floor(now() / 3600) * 3600;
const hoursAgo = (hours: number) => now() - hours * 3600;
const isoHoursAgo = (hours: number) => new Date(hoursAgo(hours) * 1000).toISOString();
let sourceUpdatesCheckedAt = now() - 120;
let memoryConfigRevision = 1;
let memoryRuntimeSequence = 1;
let memoryRuntimeId = 'mock-memory-runtime-1';
let pendingMemoryRuntime: { activateAt: number; runtimeId: string } | null = null;
let memoryConfigValues: MemoryConfigObject = {
  enabled: true,
  bot_language: 'zh',
  provider_settings: { embedding_provider_id: 'embedding-main', llm_provider_id: '' },
  recall_engine: { top_k: 5, importance_weight: 1, fallback_to_vector: true },
};
const memoryConfigSchema: MemoryConfigData['schema'] = {
  memory: {
    type: 'object',
    description: '长期记忆',
    items: {
      enabled: { type: 'bool', description: '启用长期记忆', default: true },
      bot_language: { type: 'string', description: '机器人回复语言', options: ['zh', 'en', 'ru'], default: 'zh' },
      provider_settings: {
        type: 'object', description: '模型提供商', items: {
          embedding_provider_id: { type: 'string', description: 'Embedding 模型 ID', provider_type: 'embedding', default: '' },
          llm_provider_id: { type: 'string', description: 'LLM 模型 ID', _special: 'select_provider', default: '' },
        },
      },
      recall_engine: {
        type: 'object', description: '记忆召回', items: {
          top_k: { type: 'int', description: '单次召回数量', default: 5 },
          importance_weight: { type: 'float', description: '重要性权重', default: 1 },
          fallback_to_vector: { type: 'bool', description: '降级到纯向量检索', default: true },
        },
      },
    },
  },
};

function configObject(value: MemoryConfigObject[string]): MemoryConfigObject {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as MemoryConfigObject : {};
}

function normalizeMockMemoryConfig(config: MemoryConfigObject): MemoryConfigObject {
  const providerSettings = configObject(config.provider_settings);
  const recallEngine = configObject(config.recall_engine);
  const language = typeof config.bot_language === 'string' && ['zh', 'en', 'ru'].includes(config.bot_language)
    ? config.bot_language
    : 'zh';
  return {
    enabled: config.enabled === true,
    bot_language: language,
    provider_settings: {
      embedding_provider_id: typeof providerSettings.embedding_provider_id === 'string' ? providerSettings.embedding_provider_id : '',
      llm_provider_id: typeof providerSettings.llm_provider_id === 'string' ? providerSettings.llm_provider_id : '',
    },
    recall_engine: {
      top_k: typeof recallEngine.top_k === 'number' ? Math.max(0, Math.round(recallEngine.top_k)) : 5,
      importance_weight: typeof recallEngine.importance_weight === 'number' ? recallEngine.importance_weight : 1,
      fallback_to_vector: recallEngine.fallback_to_vector !== false,
    },
  };
}

function memoryConfigData(): MemoryConfigData {
  if (pendingMemoryRuntime && Date.now() >= pendingMemoryRuntime.activateAt) {
    memoryRuntimeId = pendingMemoryRuntime.runtimeId;
    pendingMemoryRuntime = null;
  }
  return {
    schema: clone(memoryConfigSchema),
    config: clone(memoryConfigValues),
    values: clone(memoryConfigValues),
    revision: `mock-memory-${memoryConfigRevision}`,
    runtime_id: memoryRuntimeId,
    runtime_generation: memoryRuntimeSequence,
    reload_status: pendingMemoryRuntime ? 'scheduled' : 'idle',
    reload_failed: false,
    providers: {
      llm: [{ id: 'llm-main', label: 'Mock LLM', model: 'mock-chat', type: 'llm' }],
      embedding: [{ id: 'embedding-main', label: 'Mock Embedding', model: 'mock-embed', type: 'embedding' }],
    },
    constraints: {
      'recall_engine.top_k': { min: 0, max: 50, step: 1 },
      'recall_engine.importance_weight': { min: 0, max: 10, step: 0.1 },
    },
  };
}

function sourceUpdatesData(force = false): SourceUpdatesData {
  if (force) sourceUpdatesCheckedAt = now();
  return {
    checked_at: sourceUpdatesCheckedAt,
    next_check_at: sourceUpdatesCheckedAt + 300,
    refresh_allowed_at: force ? sourceUpdatesCheckedAt + 60 : sourceUpdatesCheckedAt,
    rate_limit: { limit: 60, remaining: 54, reset_at: sourceUpdatesCheckedAt + 1800 },
    sources: [
      {
        id: 'livingmemory',
        display_name: 'LivingMemory',
        role: '长期记忆来源，提供记忆存储、检索与图谱能力',
        status: 'new_commits',
        stale: false,
        baseline: {
          version: '2.3.6',
          commit_sha: 'fdcdaa063c43dad29f176eeede9cb1c54e325470',
          repository: 'lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory',
          branch: 'master',
        },
        upstream: {
          version: '2.3.6',
          commit_sha: 'e2ac45d4bdb0',
          committed_at: hoursAgo(5),
          commit_title: '更新 LivingMemory 来源实现',
          repository_url: 'https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory',
          commit_url: 'https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory/commit/e2ac45d4bdb0',
        },
        error: null,
      },
      {
        id: 'mcgetter_enhanced',
        display_name: 'MCGetter Enhanced',
        role: 'Minecraft 来源，提供服务器查询、管理与趋势能力',
        status: 'current',
        stale: false,
        baseline: {
          version: 'v1.5.0',
          commit_sha: '731cc450a44deed185c336fcabc5cd4fbd832f59',
          repository: 'exynos967/astrbot_mcgetter_enhanced',
          branch: 'main',
        },
        upstream: {
          version: 'v1.5.0',
          commit_sha: '731cc450a44deed185c336fcabc5cd4fbd832f59',
          committed_at: hoursAgo(48),
          commit_title: 'release: v1.5.0',
          repository_url: 'https://github.com/exynos967/astrbot_mcgetter_enhanced',
          commit_url: 'https://github.com/exynos967/astrbot_mcgetter_enhanced/commit/731cc450a44deed185c336fcabc5cd4fbd832f59',
        },
        error: null,
      },
    ],
  };
}

const mockOwners: MemoryIdentitiesData = {
  owners: [
    {
      owner_user_id: 'owner-zhouyi',
      display_name: '周易',
      status: 'active',
      created_at: hoursAgo(720),
      updated_at: hoursAgo(2),
      expected_updated_at: isoHoursAgo(2),
      aliases: [
        {
          identity_link_id: '1',
          owner_user_id: 'owner-zhouyi',
          platform_id: 'qq',
          bot_id: 'main',
          external_user_id: '10001',
          verified: true,
          source: 'manual',
          status: 'active',
          created_at: hoursAgo(720),
          updated_at: hoursAgo(2),
        },
        {
          identity_link_id: '2',
          owner_user_id: 'owner-zhouyi',
          platform_id: 'telegram',
          bot_id: 'main',
          external_user_id: 'zhouyi',
          verified: true,
          source: 'manual',
          status: 'active',
          created_at: hoursAgo(48),
          updated_at: hoursAgo(2),
        },
      ],
    },
    {
      owner_user_id: 'owner-xingyao',
      display_name: '星瑶',
      status: 'active',
      aliases: [],
      created_at: hoursAgo(200),
      updated_at: hoursAgo(4),
      expected_updated_at: isoHoursAgo(4),
    },
  ],
  unmapped_aliases: [],
  total: 2,
};

let mockObjectSequence = 3;
const mockObjects = new Map<string, MemoryObject>([
  ['mem-1', { memory_item_id: 'mem-1', owner_user_id: 'owner-zhouyi', owner_display_name: '周易', scope: 'persona', session_id: null, persona_id: 'default', memory_type: 'PREFERENCE', canonical_key: 'favorite_food', status: 'active', content: '喜欢南瓜汤和草莓蛋糕。', structured_payload: null, current_revision_no: 2, version: 2, importance: .9, confidence: .95, useful_score: 6, group_safe: false, current_document_id: 41, index_status: 'synced', conflict_count: 0, source_count: 2, relation_count: 1, created_at: hoursAgo(500), updated_at: hoursAgo(6) }],
  ['mem-2', { memory_item_id: 'mem-2', owner_user_id: 'owner-zhouyi', owner_display_name: '周易', scope: 'user', session_id: null, persona_id: null, memory_type: 'IDENTITY', canonical_key: 'display_name', status: 'conflicted', content: '常用称呼是周易。', structured_payload: null, current_revision_no: 1, version: 1, importance: .8, confidence: .75, useful_score: 3, group_safe: true, current_document_id: 42, index_status: 'needs_repair', conflict_count: 1, source_count: 1, relation_count: 1, created_at: hoursAgo(300), updated_at: hoursAgo(12) }],
  ['mem-3', { memory_item_id: 'mem-3', owner_user_id: 'owner-xingyao', owner_display_name: '星瑶', scope: 'persona', session_id: null, persona_id: 'default', memory_type: 'RELATIONSHIP', canonical_key: 'guardian', status: 'active', content: '把周易视为爸爸。', structured_payload: null, current_revision_no: 1, version: 1, importance: 1, confidence: .98, useful_score: 8, group_safe: true, current_document_id: 43, index_status: 'pending', conflict_count: 0, source_count: 1, relation_count: 0, created_at: hoursAgo(200), updated_at: hoursAgo(4) }],
  ['mem-2-alt', { memory_item_id: 'mem-2-alt', owner_user_id: 'owner-zhouyi', owner_display_name: '周易', scope: 'user', session_id: null, persona_id: null, memory_type: 'IDENTITY', canonical_key: 'display_name_alt', status: 'conflicted', content: '常用称呼是小周。', structured_payload: null, current_revision_no: 2, version: 2, importance: .8, confidence: .7, useful_score: 1, group_safe: true, current_document_id: 44, index_status: 'synced', conflict_count: 1, source_count: 0, relation_count: 1, created_at: hoursAgo(280), updated_at: hoursAgo(10) }],
]);
const mockRevisions = new Map<string, MemoryRevision[]>([
  ['mem-1', [
    { memory_item_id: 'mem-1', revision_no: 1, operation: 'create', content: '喜欢南瓜汤。', structured_payload: null, base_version: null, actor: 'migration', reason: '旧数据迁移', created_at: hoursAgo(500) },
    { memory_item_id: 'mem-1', revision_no: 2, operation: 'update', content: '喜欢南瓜汤和草莓蛋糕。', structured_payload: null, base_version: 1, actor: 'dashboard', reason: '补充偏好', created_at: hoursAgo(6) },
  ]],
]);
const mockSources = new Map<string, MemorySourceMessage[]>([
  ['mem-1', [{ source_id: 'source-1', revision_no: 2, source_type: 'message_window', document_id: 41, message_id_start: '100', message_id_end: '108', session_id: 'aiocqhttp:FriendMessage:10001', platform_id: 'qq', content_snapshot: '用户提到喜欢南瓜汤，之后补充草莓蛋糕。', availability: 'available', created_at: hoursAgo(6) }]],
]);
const mockConflicts: MemoryConflict[] = [{
  conflict_id: 'conflict-1', owner_user_id: 'owner-zhouyi', conflict_type: 'identity_mismatch', severity: 'high', status: 'open',
  left_item: mockObjects.get('mem-2')!,
  right_item: mockObjects.get('mem-2-alt')!,
  resolution: null, resolution_reason: null, created_at: hoursAgo(12), resolved_at: null,
}];
const mockMaintenance: MemoryMaintenanceStatusData = {
  migration: { state: 'running', processed: 72, total: 100, created: 43, deduped: 18, skipped: 8, conflicted: 3, errors: 0, unresolved_owner_count: 5 },
  index: { state: 'degraded', synced_count: 1, pending_count: 1, needs_repair_count: 1, disabled_count: 0, last_success_at: hoursAgo(1), last_error: 'mock vector index unavailable' },
  sources: { total_items: 3, covered_items: 2, partial_items: 1, unavailable_items: 0, coverage_ratio: 2 / 3 },
};

function mockObjectDetail(item: MemoryObject) {
  return {
    item,
    relations: item.memory_item_id === 'mem-1' ? [{ relation_id: 'rel-1', relation_type: 'related_to', source_memory_item_id: 'mem-1', target_memory_item_id: 'mem-3', target_content: '把周易视为爸爸。', created_at: hoursAgo(4) }] : [],
    conflicts: mockConflicts.filter((conflict) => conflict.left_item.memory_item_id === item.memory_item_id || conflict.right_item.memory_item_id === item.memory_item_id),
  };
}

const groups = ['10001', '10002'];
const serverDb: Record<string, Record<string, SavedServer>> = {
  '10001': {
    '1': {
      id: 1,
      name: '暮色群岛',
      host: 'play.example.test:25565',
      created_time: hoursAgo(720),
      last_success_time: hoursAgo(1),
      last_failed_time: null,
      failed_count: 0,
    },
    '2': {
      id: 2,
      name: '红石工坊',
      host: 'redstone.example.test:25565',
      created_time: hoursAgo(480),
      last_success_time: hoursAgo(260),
      last_failed_time: hoursAgo(12),
      failed_count: 4,
    },
  },
  '10002': {
    '1': {
      id: 1,
      name: '建筑测试区',
      host: 'build.example.test:25565',
      created_time: hoursAgo(220),
      last_success_time: hoursAgo(3),
      last_failed_time: hoursAgo(1),
      failed_count: 1,
    },
  },
};

const trendDb: Record<string, Record<string, RawTrendPoint[]>> = {
  '10001': { '1': [] },
  '10002': { '1': [] },
};

function seedTrend(groupId: string, serverId: string, offset: number) {
  const points: RawTrendPoint[] = [];
  const current = bucketNow();
  for (let index = 167; index >= 0; index -= 1) {
    if (index % (19 + offset) === 0) continue;
    const count = Math.max(0, Math.round(11 + Math.sin((168 - index + offset) / 5) * 7 + offset * 3));
    points.push({ ts: current - index * 3600, count });
  }
  trendDb[groupId] ??= {};
  trendDb[groupId][serverId] = points;
}

seedTrend('10001', '1', 0);
seedTrend('10002', '1', 1);

function clone<T>(value: T): T {
  return structuredClone(value);
}

const settingKeys = Object.keys(globalSettings) as RuntimeSettingKey[];

function effectiveSettings(groupId: string): RuntimeSettings {
  return { ...globalSettings, ...(groupOverrides[groupId] ?? {}) };
}

function settingsData(groupId: string): SettingsData {
  return {
    group_id: groupId,
    global: clone(globalSettings),
    group_overrides: clone(groupOverrides[groupId] ?? {}),
    effective: effectiveSettings(groupId),
    revision: { global: globalRevision, group: groupRevisions[groupId] ?? 0 },
    constraints: clone(settingsConstraints),
  };
}

function affectedGroupIds(input: SettingsMutationInput, key: GroupRuntimeSettingKey): string[] {
  if (input.scope === 'group') return input.group_id ? [input.group_id] : [];
  return groups.filter((groupId) => groupOverrides[groupId]?.[key] === undefined);
}

function nextSettings(input: SettingsMutationInput) {
  const groupId = input.group_id ?? groups[0] ?? '';
  const current = input.scope === 'global' ? clone(globalSettings) : effectiveSettings(groupId);
  const next = clone(current);
  if (input.scope === 'group') {
    for (const key of input.reset_keys) next[key] = globalSettings[key] as never;
  }
  for (const [key, value] of Object.entries(input.values) as Array<[RuntimeSettingKey, RuntimeSettings[RuntimeSettingKey]]>) {
    next[key] = value as never;
  }
  return { current, next };
}

function countTrim(groupIds: string[], nextLimit: number) {
  let affectedServers = 0;
  let pointsToDelete = 0;
  for (const groupId of groupIds) {
    for (const points of Object.values(trendDb[groupId] ?? {})) {
      const extra = Math.max(0, points.length - nextLimit);
      if (extra > 0) affectedServers += 1;
      pointsToDelete += extra;
    }
  }
  return { affectedServers, pointsToDelete };
}

function settingsMutationSignature(input: SettingsMutationInput) {
  return JSON.stringify({
    scope: input.scope,
    group_id: input.group_id,
    values: input.values,
    reset_keys: input.reset_keys,
    expected_revision: input.expected_revision,
  });
}

function previewSettings(input: SettingsMutationInput, issuePreviewId = false): SettingsPreviewData | ApiEnvelope<never> {
  const groupId = input.group_id ?? groups[0] ?? '';
  if (!serversFor(groupId)) return fail('GROUP_NOT_FOUND', '群组不存在');
  const expected = input.scope === 'global' ? globalRevision : groupRevisions[groupId];
  if (input.expected_revision !== expected) return fail('SETTINGS_REVISION_CONFLICT', '运行配置已被其他操作更新，请重新加载');
  if (input.scope === 'group' && ('max_concurrent_queries' in input.values || input.reset_keys.includes('max_concurrent_queries'))) {
    return fail('INVALID_SETTINGS_SCOPE', 'max_concurrent_queries 仅允许在全局范围设置');
  }

  const { current, next } = nextSettings(input);
  const historyGroups = affectedGroupIds(input, 'max_history_points');
  const trim = next.max_history_points < current.max_history_points
    ? countTrim(historyGroups, next.max_history_points)
    : { affectedServers: 0, pointsToDelete: 0 };
  const cleanupGroups = affectedGroupIds(input, 'auto_cleanup_days');
  const currentCandidates = cleanupGroups.reduce((sum, id) => sum + cleanupCandidates(id, effectiveSettings(id).auto_cleanup_days).length, 0);
  const nextCandidates = cleanupGroups.reduce((sum, id) => {
    const days = input.scope === 'global'
      ? (groupOverrides[id]?.auto_cleanup_days ?? next.auto_cleanup_days)
      : next.auto_cleanup_days;
    return sum + cleanupCandidates(id, days).length;
  }, 0);

  const previewId = issuePreviewId ? `mock-settings-preview-${++previewSequence}` : (input.preview_id ?? '');
  if (issuePreviewId) {
    settingsPreviews.set(previewId, {
      signature: settingsMutationSignature(input),
      pointsToDelete: trim.pointsToDelete,
    });
  }

  return {
    preview_id: previewId,
    current_effective: current,
    next_effective: next,
    requires_confirmation: trim.pointsToDelete > 0,
    history_trim: {
      required: trim.pointsToDelete > 0,
      current_limit: current.max_history_points,
      next_limit: next.max_history_points,
      affected_groups: input.scope === 'global' ? historyGroups : undefined,
      affected_servers: trim.affectedServers,
      points_to_delete: trim.pointsToDelete,
    },
    cleanup_impact: {
      current_candidate_count: currentCandidates,
      next_candidate_count: nextCandidates,
      new_candidate_count: Math.max(0, nextCandidates - currentCandidates),
    },
    revision: { global: globalRevision, group: groupRevisions[groupId] ?? 0 },
  };
}

function ok<T>(data: T): ApiEnvelope<T> {
  return { success: true, data: clone(data) };
}

function fail<T>(code: string, message: string): ApiEnvelope<T> {
  return { success: false, error: { code, message } };
}

async function wait(signal?: AbortSignal) {
  if (signal?.aborted) throw new DOMException('请求已取消', 'AbortError');
  await new Promise<void>((resolve, reject) => {
    const finish = () => {
      signal?.removeEventListener('abort', onAbort);
      resolve();
    };
    const timer = globalThis.setTimeout(finish, 120);
    const onAbort = () => {
      globalThis.clearTimeout(timer);
      reject(new DOMException('请求已取消', 'AbortError'));
    };
    signal?.addEventListener('abort', onAbort, { once: true });
  });
}

function serversFor(groupId: string) {
  return serverDb[groupId];
}

function findServer(groupId: string, identifier: unknown): [string, SavedServer] | null {
  const servers = serversFor(groupId);
  if (!servers) return null;
  const value = String(identifier ?? '');
  if (servers[value]) return [value, servers[value]];
  const entry = Object.entries(servers).find(([, server]) => server.name === value);
  return entry ?? null;
}

function nextId(groupId: string) {
  const ids = Object.keys(serversFor(groupId) ?? {}).map(Number).filter(Number.isFinite);
  return (ids.length ? Math.max(...ids) : 0) + 1;
}

function cleanupCandidates(groupId: string, cleanupDays = effectiveSettings(groupId).auto_cleanup_days): CleanupCandidate[] {
  const cutoff = now() - cleanupDays * 24 * 3600;
  return Object.entries(serversFor(groupId) ?? {}).flatMap(([id, server]) => {
    const history = trendDb[groupId]?.[id] ?? [];
    const latestTrend = history.at(-1)?.ts ?? 0;
    const effective = Math.max(server.last_success_time ?? 0, latestTrend);
    if (effective >= cutoff) return [];
    return [{
      id,
      name: server.name,
      host: server.host,
      last_success_time: server.last_success_time,
      effective_last_success_time: effective || null,
      failed_count: server.failed_count,
    }];
  });
}

function appendTrend(groupId: string, serverId: string, count: number) {
  trendDb[groupId] ??= {};
  trendDb[groupId][serverId] ??= [];
  const bucket = bucketNow();
  const history = trendDb[groupId][serverId];
  const existing = history.find((point) => point.ts === bucket);
  if (existing) existing.count = count;
  else history.push({ ts: bucket, count });
}

function bodyRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function requiredText(value: unknown): string {
  return typeof value === 'string' ? value.trim() : '';
}

function mockOwner(ownerUserId: string) {
  return mockOwners.owners.find((owner) => owner.owner_user_id === ownerUserId);
}

function mockOwnerItem(ownerUserId: string, memoryItemId: string) {
  const item = mockObjects.get(memoryItemId);
  return item?.owner_user_id === ownerUserId ? item : undefined;
}

function selectedVersionedObjects(
  ownerUserId: string,
  memoryItemIds: string[],
  expectedVersions: unknown,
): MemoryObject[] | ApiEnvelope<never> {
  if (!expectedVersions || typeof expectedVersions !== 'object' || Array.isArray(expectedVersions)) {
    return fail('MEMORY_INVALID_REQUEST', 'expected_versions 必须是对象');
  }
  const versions = expectedVersions as Record<string, unknown>;
  const selected: MemoryObject[] = [];
  for (const memoryItemId of memoryItemIds) {
    const item = mockOwnerItem(ownerUserId, memoryItemId);
    if (!item) return fail('MEMORY_OBJECT_NOT_FOUND', '记忆对象不存在');
    const expectedVersion = versions[memoryItemId];
    if (!Number.isInteger(expectedVersion) || Number(expectedVersion) < 1) {
      return fail('MEMORY_INVALID_REQUEST', `缺少 expected_version: ${memoryItemId}`);
    }
    if (expectedVersion !== item.version) {
      return fail('MEMORY_REVISION_CONFLICT', '对象已被其他操作更新，请加载最新版本');
    }
    selected.push(item);
  }
  return selected;
}

function mockOwnerMergeState(ownerUserId: string): Record<string, string> {
  const owner = mockOwner(ownerUserId);
  if (!owner) return {};
  const aliases = owner.aliases;
  const items = [...mockObjects.values()].filter((item) => item.owner_user_id === ownerUserId);
  const conflicts = mockConflicts.filter((conflict) => conflict.owner_user_id === ownerUserId);
  return {
    status: owner.status,
    updated_at: owner.expected_updated_at,
    alias_count: String(aliases.length),
    alias_updated_at: String(Math.max(0, ...aliases.map((alias) => alias.updated_at ?? 0))),
    memory_item_count: String(items.length),
    memory_item_updated_at: String(Math.max(0, ...items.map((item) => item.updated_at ?? 0))),
    memory_version_sum: String(items.reduce((sum, item) => sum + item.version, 0)),
    conflict_count: String(conflicts.length),
    conflict_updated_at: String(Math.max(0, ...conflicts.map((conflict) => conflict.resolved_at ?? conflict.created_at ?? 0))),
  };
}

const ownerMergePreviews = new Map<string, {
  survivorOwnerUserId: string;
  sourceOwnerUserIds: string[];
  expectedOwnerStates: Record<string, Record<string, string>>;
}>();
let ownerMergePreviewSequence = 0;
let mockOwnerSequence = 2;
let mockAliasSequence = 2;

function sameJson(left: unknown, right: unknown) {
  return JSON.stringify(left) === JSON.stringify(right);
}

async function mockMemoryRequest<T>(
  path: string,
  method: 'GET' | 'POST',
  query: Query | undefined,
  body: unknown,
): Promise<ApiEnvelope<T>> {
  const input = bodyRecord(body);

  if (method === 'GET' && path === MEMORY_ADMIN_PATHS.objects) {
    const ownerUserId = requiredText(query?.owner_user_id);
    if (!ownerUserId) return fail('MEMORY_INVALID_REQUEST', 'owner_user_id 必填');
    if (!mockOwner(ownerUserId)) return fail('MEMORY_OBJECT_NOT_FOUND', 'owner 不存在');
    const page = Number(query?.page ?? 1);
    const pageSize = Number(query?.page_size ?? 20);
    if (!Number.isInteger(page) || page < 1 || !Number.isInteger(pageSize) || pageSize < 1 || pageSize > 500) {
      return fail('MEMORY_INVALID_REQUEST', '分页参数无效');
    }
    const keyword = String(query?.keyword ?? '').toLowerCase();
    const filtered = [...mockObjects.values()].filter((item) => {
      if (item.owner_user_id !== ownerUserId) return false;
      if (keyword && !`${item.content} ${item.canonical_key ?? ''} ${item.memory_item_id}`.toLowerCase().includes(keyword)) return false;
      if (query?.scope && item.scope !== query.scope) return false;
      if (query?.persona_id && item.persona_id !== query.persona_id) return false;
      if (query?.status && item.status !== query.status) return false;
      if (query?.memory_type && item.memory_type !== query.memory_type) return false;
      if (query?.conflict === 'yes' && item.conflict_count === 0) return false;
      if (query?.conflict === 'no' && item.conflict_count > 0) return false;
      if (query?.index_status && item.index_status !== query.index_status) return false;
      return true;
    });
    const start = (page - 1) * pageSize;
    return ok({
      items: filtered.slice(start, start + pageSize),
      total: filtered.length,
      page,
      page_size: pageSize,
      has_more: start + pageSize < filtered.length,
    }) as ApiEnvelope<T>;
  }

  if (method === 'GET' && path === MEMORY_ADMIN_PATHS.objectDetail) {
    const ownerUserId = requiredText(query?.owner_user_id);
    const memoryItemId = requiredText(query?.memory_item_id);
    if (!ownerUserId || !memoryItemId) return fail('MEMORY_INVALID_REQUEST', 'owner_user_id 和 memory_item_id 必填');
    const item = mockOwnerItem(ownerUserId, memoryItemId);
    return item
      ? ok(mockObjectDetail(item)) as ApiEnvelope<T>
      : fail('MEMORY_OBJECT_NOT_FOUND', '记忆对象不存在') as ApiEnvelope<T>;
  }

  if (method === 'GET' && path === MEMORY_ADMIN_PATHS.objectRevisions) {
    const ownerUserId = requiredText(query?.owner_user_id);
    const memoryItemId = requiredText(query?.memory_item_id);
    if (!ownerUserId || !memoryItemId) return fail('MEMORY_INVALID_REQUEST', 'owner_user_id 和 memory_item_id 必填');
    const item = mockOwnerItem(ownerUserId, memoryItemId);
    if (!item) return fail('MEMORY_OBJECT_NOT_FOUND', '记忆对象不存在');
    return ok({
      revisions: mockRevisions.get(memoryItemId) ?? [{
        memory_item_id: memoryItemId,
        revision_no: item.current_revision_no,
        operation: 'create',
        content: item.content,
        structured_payload: null,
        base_version: null,
        actor: 'mock',
        reason: null,
        created_at: item.created_at,
      }],
    }) as ApiEnvelope<T>;
  }

  if (method === 'GET' && path === MEMORY_ADMIN_PATHS.objectSources) {
    const ownerUserId = requiredText(query?.owner_user_id);
    const memoryItemId = requiredText(query?.memory_item_id);
    if (!ownerUserId || !memoryItemId) return fail('MEMORY_INVALID_REQUEST', 'owner_user_id 和 memory_item_id 必填');
    if (!mockOwnerItem(ownerUserId, memoryItemId)) return fail('MEMORY_OBJECT_NOT_FOUND', '记忆对象不存在');
    const revisionNo = query?.revision_no === undefined ? undefined : Number(query.revision_no);
    const sources = mockSources.get(memoryItemId) ?? [];
    return ok({
      sources: revisionNo === undefined
        ? sources
        : sources.filter((source) => source.revision_no === revisionNo),
    }) as ApiEnvelope<T>;
  }

  if (method === 'POST' && path === MEMORY_ADMIN_PATHS.objectCreate) {
    const ownerUserId = requiredText(input.owner_user_id);
    const content = requiredText(input.content);
    if (!ownerUserId || !content || input.expected_version === undefined) {
      return fail('MEMORY_INVALID_REQUEST', 'owner_user_id、content 和 expected_version 必填');
    }
    if (input.expected_version !== 0) return fail('MEMORY_REVISION_CONFLICT', '新建对象 expected_version 必须为 0');
    const owner = mockOwner(ownerUserId);
    if (!owner) return fail('MEMORY_OBJECT_NOT_FOUND', 'owner 不存在');
    const typedInput = input as unknown as MemoryObjectMutationInput;
    const id = `mem-${++mockObjectSequence}`;
    const item: MemoryObject = {
      memory_item_id: id,
      owner_user_id: ownerUserId,
      owner_display_name: owner.display_name,
      scope: typedInput.scope ?? 'persona',
      session_id: typedInput.session_id ?? null,
      persona_id: typedInput.persona_id ?? null,
      memory_type: typedInput.memory_type || 'fact',
      canonical_key: typedInput.canonical_key ?? null,
      status: 'active',
      content,
      structured_payload: typedInput.structured_payload ?? null,
      current_revision_no: 1,
      version: 1,
      importance: typedInput.importance ?? .5,
      confidence: typedInput.confidence ?? .7,
      useful_score: 0,
      group_safe: typedInput.group_safe ?? false,
      current_document_id: null,
      index_status: 'pending',
      conflict_count: 0,
      source_count: 0,
      relation_count: 0,
      created_at: now(),
      updated_at: now(),
    };
    mockObjects.set(id, item);
    mockRevisions.set(id, [{
      memory_item_id: id,
      revision_no: 1,
      operation: 'create',
      content: item.content,
      structured_payload: item.structured_payload,
      base_version: null,
      actor: 'dashboard',
      reason: typedInput.reason ?? null,
      created_at: now(),
    }]);
    return ok(mockObjectDetail(item)) as ApiEnvelope<T>;
  }

  if (method === 'POST' && path === MEMORY_ADMIN_PATHS.objectUpdate) {
    const ownerUserId = requiredText(input.owner_user_id);
    const memoryItemId = requiredText(input.memory_item_id);
    const expectedVersion = input.expected_version;
    if (!ownerUserId || !memoryItemId || !Number.isInteger(expectedVersion) || Number(expectedVersion) < 1) {
      return fail('MEMORY_INVALID_REQUEST', 'owner_user_id、memory_item_id 和 expected_version 必填');
    }
    const current = mockOwnerItem(ownerUserId, memoryItemId);
    if (!current) return fail('MEMORY_OBJECT_NOT_FOUND', '记忆对象不存在');
    if (expectedVersion !== current.version) return fail('MEMORY_REVISION_CONFLICT', '对象已被其他操作更新，请加载最新版本');
    const targetOwnerUserId = requiredText(input.target_owner_user_id);
    if (targetOwnerUserId && targetOwnerUserId !== ownerUserId) {
      return fail('MEMORY_ACCESS_CONTEXT_INVALID', '对象更新禁止跨 owner 移动；请使用 owner merge');
    }
    if ('canonical_key' in input && !requiredText(input.canonical_key)) {
      return fail('MEMORY_INVALID_REQUEST', 'canonical_key 为空时必须省略该字段');
    }
    const typedInput = input as unknown as MemoryObjectUpdateInput;
    const next: MemoryObject = {
      ...current,
      scope: typedInput.scope ?? current.scope,
      content: typedInput.content ?? current.content,
      session_id: typedInput.session_id === undefined ? current.session_id : typedInput.session_id,
      persona_id: typedInput.persona_id === undefined ? current.persona_id : typedInput.persona_id,
      memory_type: typedInput.memory_type ?? current.memory_type,
      canonical_key: typedInput.canonical_key === undefined ? current.canonical_key : typedInput.canonical_key,
      structured_payload: typedInput.structured_payload === undefined ? current.structured_payload : typedInput.structured_payload,
      importance: typedInput.importance ?? current.importance,
      confidence: typedInput.confidence ?? current.confidence,
      group_safe: typedInput.group_safe ?? current.group_safe,
      version: current.version + 1,
      current_revision_no: current.current_revision_no + 1,
      updated_at: now(),
    };
    mockObjects.set(next.memory_item_id, next);
    const revisions = mockRevisions.get(next.memory_item_id) ?? [];
    revisions.push({
      memory_item_id: next.memory_item_id,
      revision_no: next.current_revision_no,
      operation: 'update',
      content: next.content,
      structured_payload: next.structured_payload,
      base_version: current.version,
      actor: 'dashboard',
      reason: typedInput.reason ?? null,
      created_at: now(),
    });
    mockRevisions.set(next.memory_item_id, revisions);
    return ok(mockObjectDetail(next)) as ApiEnvelope<T>;
  }

  if (method === 'POST' && path === MEMORY_ADMIN_PATHS.objectArchive) {
    const ownerUserId = requiredText(input.owner_user_id);
    const memoryItemId = requiredText(input.memory_item_id);
    const expectedVersion = input.expected_version;
    if (!ownerUserId || !memoryItemId || !Number.isInteger(expectedVersion) || Number(expectedVersion) < 1) {
      return fail('MEMORY_INVALID_REQUEST', 'owner_user_id、memory_item_id 和 expected_version 必填');
    }
    const current = mockOwnerItem(ownerUserId, memoryItemId);
    if (!current) return fail('MEMORY_OBJECT_NOT_FOUND', '记忆对象不存在');
    if (expectedVersion !== current.version) return fail('MEMORY_REVISION_CONFLICT', '对象版本冲突');
    const next = {
      ...current,
      status: 'archived' as const,
      version: current.version + 1,
      current_revision_no: current.current_revision_no + 1,
      updated_at: now(),
    };
    mockObjects.set(next.memory_item_id, next);
    return ok(mockObjectDetail(next)) as ApiEnvelope<T>;
  }

  if (method === 'POST' && path === MEMORY_ADMIN_PATHS.objectBatch) {
    const ownerUserId = requiredText(input.owner_user_id);
    const action = requiredText(input.action);
    const items = input.items;
    if (!ownerUserId || !Array.isArray(items) || !items.length) {
      return fail('MEMORY_INVALID_REQUEST', 'owner_user_id 和 items 必填');
    }
    if (action !== 'archive' && action !== 'index_retry') {
      return fail('MEMORY_VALIDATION_FAILED', 'action 必须是 archive 或 index_retry');
    }
    const selected: Array<{ item: MemoryObject; expectedVersion: number }> = [];
    for (const raw of items) {
      const value = bodyRecord(raw);
      const memoryItemId = requiredText(value.memory_item_id);
      const expectedVersion = value.expected_version;
      if (!memoryItemId || !Number.isInteger(expectedVersion) || Number(expectedVersion) < 1) {
        return fail('MEMORY_INVALID_REQUEST', 'items 必须携带 memory_item_id 和 expected_version');
      }
      const item = mockOwnerItem(ownerUserId, memoryItemId);
      if (!item) return fail('MEMORY_OBJECT_NOT_FOUND', '记忆对象不存在');
      if (expectedVersion !== item.version) return fail('MEMORY_REVISION_CONFLICT', `对象 ${memoryItemId} 版本冲突`);
      selected.push({ item, expectedVersion: Number(expectedVersion) });
    }
    selected.forEach(({ item }) => {
      mockObjects.set(item.memory_item_id, {
        ...item,
        status: action === 'archive' ? 'archived' : item.status,
        index_status: action === 'index_retry' ? 'pending' : item.index_status,
        version: item.version + (action === 'archive' ? 1 : 0),
        updated_at: now(),
      });
    });
    return ok({ updated_count: selected.length, action }) as ApiEnvelope<T>;
  }

  if (method === 'POST' && (path === MEMORY_ADMIN_PATHS.objectMergePreview || path === MEMORY_ADMIN_PATHS.objectMerge)) {
    const ownerUserId = requiredText(input.owner_user_id);
    const survivorMemoryItemId = requiredText(input.survivor_memory_item_id);
    const sourceMemoryItemIds = Array.isArray(input.source_memory_item_ids)
      ? input.source_memory_item_ids.map(requiredText).filter(Boolean)
      : [];
    if (!ownerUserId || !survivorMemoryItemId || !sourceMemoryItemIds.length) {
      return fail('MEMORY_INVALID_REQUEST', 'owner_user_id、survivor_memory_item_id 和 source_memory_item_ids 必填');
    }
    const ids = [survivorMemoryItemId, ...sourceMemoryItemIds];
    const selected = selectedVersionedObjects(ownerUserId, ids, input.expected_versions);
    if (!Array.isArray(selected)) return selected as ApiEnvelope<T>;
    if (path === MEMORY_ADMIN_PATHS.objectMergePreview) {
      return ok({
        owner_user_id: ownerUserId,
        survivor_memory_item_id: survivorMemoryItemId,
        source_memory_item_ids: sourceMemoryItemIds,
        merged_content: selected.map((item) => item.content).join('\n'),
        merged_structured_payload: Object.assign(
          {},
          ...selected.map((item) => item.structured_payload ?? {}),
        ),
        warnings: [],
        expected_versions: input.expected_versions,
      }) as ApiEnvelope<T>;
    }
    const content = requiredText(input.content);
    if (!content) return fail('MEMORY_INVALID_REQUEST', 'content 必填');
    const survivor = selected[0];
    const next = {
      ...survivor,
      content,
      structured_payload: bodyRecord(input.structured_payload),
      version: survivor.version + 1,
      current_revision_no: survivor.current_revision_no + 1,
      updated_at: now(),
    };
    mockObjects.set(next.memory_item_id, next);
    sourceMemoryItemIds.forEach((memoryItemId) => {
      const source = mockOwnerItem(ownerUserId, memoryItemId)!;
      mockObjects.set(memoryItemId, { ...source, status: 'superseded', updated_at: now() });
    });
    return ok(mockObjectDetail(next)) as ApiEnvelope<T>;
  }

  if (method === 'POST' && path === MEMORY_ADMIN_PATHS.objectSupersede) {
    const ownerUserId = requiredText(input.owner_user_id);
    const oldMemoryItemId = requiredText(input.old_memory_item_id ?? input.old_item_id);
    const newMemoryItemId = requiredText(input.new_memory_item_id ?? input.replacement_item_id);
    if (!ownerUserId || !oldMemoryItemId || !newMemoryItemId) {
      return fail('MEMORY_INVALID_REQUEST', 'owner_user_id、old_memory_item_id 和 new_memory_item_id 必填');
    }
    const selected = selectedVersionedObjects(
      ownerUserId,
      [oldMemoryItemId, newMemoryItemId],
      input.expected_versions,
    );
    if (!Array.isArray(selected)) return selected as ApiEnvelope<T>;
    const oldItem = selected[0];
    const replacement = selected[1];
    mockObjects.set(oldMemoryItemId, { ...oldItem, status: 'superseded', updated_at: now() });
    const next = {
      ...replacement,
      version: replacement.version + 1,
      current_revision_no: replacement.current_revision_no + 1,
      updated_at: now(),
    };
    mockObjects.set(newMemoryItemId, next);
    return ok(mockObjectDetail(next)) as ApiEnvelope<T>;
  }

  if (method === 'GET' && path === MEMORY_ADMIN_PATHS.conflicts) {
    const ownerUserId = requiredText(query?.owner_user_id);
    if (!ownerUserId) return fail('MEMORY_INVALID_REQUEST', 'owner_user_id 必填');
    if (!mockOwner(ownerUserId)) return fail('MEMORY_OBJECT_NOT_FOUND', 'owner 不存在');
    return ok({
      conflicts: mockConflicts.filter((conflict) => (
        conflict.owner_user_id === ownerUserId
        && (!query?.status || conflict.status === query.status)
      )),
    }) as ApiEnvelope<T>;
  }

  if (method === 'POST' && path === MEMORY_ADMIN_PATHS.conflictResolve) {
    const ownerUserId = requiredText(input.owner_user_id);
    const conflictId = requiredText(input.conflict_id);
    const action = requiredText(input.action);
    if (!ownerUserId || !conflictId || !action) return fail('MEMORY_INVALID_REQUEST', 'owner_user_id、conflict_id 和 action 必填');
    const conflict = mockConflicts.find((value) => (
      value.conflict_id === conflictId && value.owner_user_id === ownerUserId
    ));
    if (!conflict) return fail('MEMORY_OBJECT_NOT_FOUND', '冲突记录不存在');
    const selected = selectedVersionedObjects(
      ownerUserId,
      [conflict.left_item.memory_item_id, conflict.right_item.memory_item_id],
      input.expected_versions,
    );
    if (!Array.isArray(selected)) return selected as ApiEnvelope<T>;
    if (!['merge', 'supersede_left', 'supersede_right', 'dismiss'].includes(action)) {
      return fail('MEMORY_VALIDATION_FAILED', '冲突处理 action 无效');
    }
    conflict.status = action === 'dismiss' ? 'dismissed' : 'resolved';
    conflict.resolution = action;
    conflict.resolved_at = now();
    return ok({ conflict }) as ApiEnvelope<T>;
  }

  if (method === 'GET' && path === MEMORY_ADMIN_PATHS.identities) {
    return ok(mockOwners) as ApiEnvelope<T>;
  }

  if (method === 'POST' && path === MEMORY_ADMIN_PATHS.ownerCreate) {
    const displayName = requiredText(input.display_name);
    if (!displayName) return fail('MEMORY_INVALID_REQUEST', 'display_name 必填');
    const ownerUserId = requiredText(input.owner_user_id) || `owner-mock-${++mockOwnerSequence}`;
    if (mockOwner(ownerUserId)) return fail('MEMORY_CONSTRAINT_CONFLICT', 'owner 已存在');
    const expectedUpdatedAt = new Date().toISOString();
    const owner = {
      owner_user_id: ownerUserId,
      display_name: displayName,
      status: 'active' as const,
      aliases: [],
      created_at: now(),
      updated_at: now(),
      expected_updated_at: expectedUpdatedAt,
    };
    mockOwners.owners.push(owner);
    mockOwners.total = mockOwners.owners.length;
    return ok({ owner, identities: mockOwners }) as ApiEnvelope<T>;
  }

  if (method === 'POST' && path === MEMORY_ADMIN_PATHS.ownerUpdate) {
    const ownerUserId = requiredText(input.owner_user_id);
    const displayName = requiredText(input.display_name) || ownerUserId;
    const status = requiredText(input.status);
    const expectedUpdatedAt = requiredText(input.expected_updated_at);
    if (!ownerUserId || !status || !expectedUpdatedAt) {
      return fail('MEMORY_INVALID_REQUEST', 'owner_user_id、status 和 expected_updated_at 必填');
    }
    const owner = mockOwner(ownerUserId);
    if (!owner) return fail('MEMORY_OBJECT_NOT_FOUND', 'owner 不存在');
    if (owner.expected_updated_at !== expectedUpdatedAt) {
      return fail('MEMORY_STATE_CONFLICT', 'owner 状态已变化，请重新加载');
    }
    if (!['active', 'merged', 'disabled'].includes(status)) {
      return fail('MEMORY_OWNER_STATUS_INVALID', 'owner status 无效');
    }
    owner.display_name = displayName;
    owner.status = status as typeof owner.status;
    owner.updated_at = now();
    owner.expected_updated_at = new Date().toISOString();
    return ok({ owner }) as ApiEnvelope<T>;
  }

  if (method === 'POST' && path === MEMORY_ADMIN_PATHS.aliasLink) {
    const ownerUserId = requiredText(input.owner_user_id);
    const platformId = requiredText(input.platform_id);
    const botId = requiredText(input.bot_id);
    const externalUserId = requiredText(input.external_user_id);
    if (!ownerUserId || !platformId || !botId || !externalUserId) {
      return fail('MEMORY_INVALID_REQUEST', 'owner_user_id 和 alias 标识必填');
    }
    const owner = mockOwner(ownerUserId);
    if (!owner) return fail('MEMORY_OBJECT_NOT_FOUND', 'owner 不存在');
    const existing = mockOwners.owners.flatMap((value) => value.aliases).find((value) => (
      value.platform_id === platformId
      && value.bot_id === botId
      && value.external_user_id === externalUserId
    ));
    if (existing && existing.owner_user_id !== ownerUserId) {
      return fail('MEMORY_ALIAS_CONFLICT', 'identity alias 已绑定到其他 owner，必须使用显式移动操作');
    }
    if (existing) return ok({ alias: existing }) as ApiEnvelope<T>;
    const alias = {
      identity_link_id: String(++mockAliasSequence),
      owner_user_id: ownerUserId,
      platform_id: platformId,
      bot_id: botId,
      external_user_id: externalUserId,
      verified: input.verified !== false,
      source: 'admin',
      status: 'active',
      created_at: now(),
      updated_at: now(),
    };
    owner.aliases.push(alias);
    return ok({ alias }) as ApiEnvelope<T>;
  }

  if (method === 'POST' && path === MEMORY_ADMIN_PATHS.aliasMove) {
    const identityLinkId = requiredText(input.identity_link_id);
    const ownerUserId = requiredText(input.owner_user_id);
    const expectedOwnerUserId = requiredText(input.expected_owner_user_id);
    if (!identityLinkId || !Number.isInteger(Number(identityLinkId)) || Number(identityLinkId) < 1 || !ownerUserId || !expectedOwnerUserId) {
      return fail('MEMORY_INVALID_REQUEST', 'identity_link_id、owner_user_id 和 expected_owner_user_id 必填');
    }
    const targetOwner = mockOwner(ownerUserId);
    const expectedOwner = mockOwner(expectedOwnerUserId);
    if (!targetOwner || !expectedOwner) return fail('MEMORY_OBJECT_NOT_FOUND', 'owner 不存在');
    const aliasIndex = expectedOwner.aliases.findIndex((value) => value.identity_link_id === identityLinkId);
    if (aliasIndex < 0) {
      const existsElsewhere = mockOwners.owners.some((value) => (
        value.aliases.some((alias) => alias.identity_link_id === identityLinkId)
      ));
      return existsElsewhere
        ? fail('MEMORY_STATE_CONFLICT', 'alias 当前 owner 已变化')
        : fail('MEMORY_OBJECT_NOT_FOUND', 'identity link 不存在');
    }
    const [previous] = expectedOwner.aliases.splice(aliasIndex, 1);
    const moved = { ...previous, owner_user_id: ownerUserId, updated_at: now() };
    targetOwner.aliases.push(moved);
    return ok({
      moved: true,
      previous_owner_user_id: expectedOwnerUserId,
      alias: moved,
    }) as ApiEnvelope<T>;
  }

  if (method === 'POST' && path === MEMORY_ADMIN_PATHS.ownerMergePreview) {
    const survivorOwnerUserId = requiredText(input.survivor_owner_user_id);
    const sourceOwnerUserIds = Array.isArray(input.source_owner_user_ids)
      ? [...new Set(input.source_owner_user_ids.map(requiredText).filter(Boolean))]
        .filter((ownerUserId) => ownerUserId !== survivorOwnerUserId)
        .sort()
      : [];
    if (!survivorOwnerUserId || !sourceOwnerUserIds.length) {
      return fail('MEMORY_INVALID_REQUEST', 'owner merge 至少需要一个来源 owner');
    }
    const ownerIds = [survivorOwnerUserId, ...sourceOwnerUserIds];
    const selectedOwners = ownerIds.map(mockOwner);
    if (selectedOwners.some((owner) => !owner)) return fail('MEMORY_OBJECT_NOT_FOUND', 'owner merge 包含不存在的 owner');
    if (selectedOwners.some((owner) => owner?.status !== 'active')) {
      return fail('MEMORY_ACCESS_CONTEXT_INVALID', '仅 active owner 可以合并');
    }
    const expectedOwnerStates = Object.fromEntries(
      [...ownerIds].sort().map((ownerUserId) => [ownerUserId, mockOwnerMergeState(ownerUserId)]),
    );
    const previewId = `owner_merge_mock_${++ownerMergePreviewSequence}`;
    ownerMergePreviews.set(previewId, {
      survivorOwnerUserId,
      sourceOwnerUserIds,
      expectedOwnerStates,
    });
    const sourceItems = [...mockObjects.values()].filter((item) => sourceOwnerUserIds.includes(item.owner_user_id));
    const sourceConflicts = mockConflicts.filter((conflict) => (
      sourceOwnerUserIds.includes(conflict.owner_user_id) && conflict.status === 'open'
    ));
    return ok({
      preview_id: previewId,
      survivor_owner_user_id: survivorOwnerUserId,
      source_owner_user_ids: sourceOwnerUserIds,
      alias_count: selectedOwners.slice(1).reduce((sum, owner) => sum + (owner?.aliases.length ?? 0), 0),
      memory_item_count: sourceItems.length,
      conflict_count: sourceConflicts.length,
      warnings: sourceConflicts.length ? ['来源 owner 存在未解决冲突，合并后仍需逐条处理'] : [],
      expected_owner_states: expectedOwnerStates,
    }) as ApiEnvelope<T>;
  }

  if (method === 'POST' && path === MEMORY_ADMIN_PATHS.ownerMerge) {
    const survivorOwnerUserId = requiredText(input.survivor_owner_user_id);
    const sourceOwnerUserIds = Array.isArray(input.source_owner_user_ids)
      ? [...new Set(input.source_owner_user_ids.map(requiredText).filter(Boolean))]
        .filter((ownerUserId) => ownerUserId !== survivorOwnerUserId)
        .sort()
      : [];
    const previewId = requiredText(input.preview_id);
    const expectedOwnerStates = bodyRecord(input.expected_owner_states) as Record<string, Record<string, string>>;
    if (!survivorOwnerUserId || !sourceOwnerUserIds.length || !previewId || !Object.keys(expectedOwnerStates).length) {
      return fail('MEMORY_INVALID_REQUEST', 'expected_owner_states 必须来自 owner merge preview');
    }
    const preview = ownerMergePreviews.get(previewId);
    const currentStates = Object.fromEntries(
      [survivorOwnerUserId, ...sourceOwnerUserIds]
        .sort()
        .map((ownerUserId) => [ownerUserId, mockOwnerMergeState(ownerUserId)]),
    );
    if (
      !preview
      || preview.survivorOwnerUserId !== survivorOwnerUserId
      || !sameJson(preview.sourceOwnerUserIds, sourceOwnerUserIds)
      || !sameJson(preview.expectedOwnerStates, expectedOwnerStates)
      || !sameJson(currentStates, expectedOwnerStates)
    ) {
      return fail('MEMORY_STATE_CONFLICT', 'owner merge preview 无效或已过期');
    }
    const survivor = mockOwner(survivorOwnerUserId);
    if (!survivor) return fail('MEMORY_OBJECT_NOT_FOUND', 'owner 不存在');
    sourceOwnerUserIds.forEach((sourceOwnerUserId) => {
      const source = mockOwner(sourceOwnerUserId)!;
      source.aliases.forEach((alias) => survivor.aliases.push({
        ...alias,
        owner_user_id: survivorOwnerUserId,
        updated_at: now(),
      }));
      source.aliases = [];
      source.status = 'merged';
      source.updated_at = now();
      source.expected_updated_at = new Date().toISOString();
      [...mockObjects.values()]
        .filter((item) => item.owner_user_id === sourceOwnerUserId)
        .forEach((item) => mockObjects.set(item.memory_item_id, {
          ...item,
          owner_user_id: survivorOwnerUserId,
          owner_display_name: survivor.display_name,
          updated_at: now(),
        }));
      mockConflicts
        .filter((conflict) => conflict.owner_user_id === sourceOwnerUserId)
        .forEach((conflict) => { conflict.owner_user_id = survivorOwnerUserId; });
    });
    survivor.updated_at = now();
    survivor.expected_updated_at = new Date().toISOString();
    ownerMergePreviews.delete(previewId);
    return ok({
      merged: true,
      survivor_owner_user_id: survivorOwnerUserId,
      source_owner_user_ids: sourceOwnerUserIds,
      identities: mockOwners,
    }) as ApiEnvelope<T>;
  }

  if (method === 'GET' && path === MEMORY_ADMIN_PATHS.maintenanceStatus) {
    return ok(mockMaintenance) as ApiEnvelope<T>;
  }

  if (method === 'POST' && path === MEMORY_ADMIN_PATHS.indexRetry) {
    const ownerUserId = requiredText(input.owner_user_id);
    const items = input.items;
    if (!ownerUserId || !Array.isArray(items) || !items.length) {
      return fail('MEMORY_INVALID_REQUEST', 'owner_user_id 和非空 items 必填');
    }
    const selected: MemoryObject[] = [];
    for (const raw of items) {
      const value = bodyRecord(raw);
      const memoryItemId = requiredText(value.memory_item_id);
      const expectedVersion = value.expected_version;
      if (!memoryItemId || !Number.isInteger(expectedVersion) || Number(expectedVersion) < 1) {
        return fail('MEMORY_INVALID_REQUEST', 'items 必须携带 memory_item_id 和 expected_version');
      }
      const item = mockOwnerItem(ownerUserId, memoryItemId);
      if (!item) return fail('MEMORY_OBJECT_NOT_FOUND', '记忆对象不存在');
      if (expectedVersion !== item.version) return fail('MEMORY_REVISION_CONFLICT', '对象版本冲突');
      selected.push(item);
    }
    selected.forEach((item) => mockObjects.set(item.memory_item_id, {
      ...item,
      index_status: 'pending',
      updated_at: now(),
    }));
    return ok({ owner_user_id: ownerUserId, queued_count: selected.length, state: 'pending' }) as ApiEnvelope<T>;
  }

  return fail('ENDPOINT_NOT_FOUND', `Mock 未实现 Memory endpoint：${path}`);
}

export async function mockRequest<T>(
  endpoint: string,
  method: 'GET' | 'POST',
  query: Query | undefined,
  body: unknown,
  signal?: AbortSignal,
): Promise<ApiEnvelope<T>> {
  await wait(signal);

  if (endpoint.startsWith('/page/v1/memory/')) {
    const path = endpoint.slice('/page/v1/memory/'.length);
    return mockMemoryRequest<T>(path, method, query, body);
  }

  if (method === 'GET' && endpoint === '/page/v1/config/memory') {
    return ok(memoryConfigData()) as ApiEnvelope<T>;
  }

  if (method === 'POST' && endpoint === '/page/v1/config/memory') {
    const input = body as MemoryConfigMutationInput;
    if (input.expected_revision !== `mock-memory-${memoryConfigRevision}`) {
      return fail('MEMORY_CONFIG_REVISION_CONFLICT', '记忆配置已被其他操作更新，请重新加载后审查更改') as ApiEnvelope<T>;
    }
    const oldRuntimeId = memoryRuntimeId;
    memoryConfigValues = normalizeMockMemoryConfig(input.config);
    memoryConfigRevision += 1;
    const nextRuntimeId = `mock-memory-runtime-${++memoryRuntimeSequence}`;
    pendingMemoryRuntime = { activateAt: Date.now() + 1_300, runtimeId: nextRuntimeId };
    const result: MemoryConfigSaveData = {
      config: clone(memoryConfigValues),
      revision: `mock-memory-${memoryConfigRevision}`,
      old_runtime_id: oldRuntimeId,
      reload_scheduled: true,
      reload_pending: true,
      reload_status: 'scheduled',
      reload_failed: false,
      manual_reload_required: false,
    };
    return ok(result) as ApiEnvelope<T>;
  }

  if (method === 'GET' && endpoint === '/page/v1/sources/updates') {
    return ok(sourceUpdatesData()) as ApiEnvelope<T>;
  }

  if (method === 'POST' && endpoint === '/page/v1/sources/updates/refresh') {
    return ok(sourceUpdatesData(true)) as ApiEnvelope<T>;
  }

  if (method === 'GET' && (endpoint === '/page/bootstrap' || endpoint === '/page/v1/bootstrap')) {
    return ok({
      brand: 'Zhouyi Dashboard',
      groups: groups.map((id) => ({ id })),
      selected_group_id: groups[0] ?? null,
      capabilities: {
        mc: { available: true, enabled: true, initialized: true, error: null },
        memory: { available: true, enabled: true, initialized: true, error: null },
      },
    }) as ApiEnvelope<T>;
  }

  if (method === 'GET' && endpoint === '/page/settings') {
    const groupId = String(query?.group_id ?? '');
    if (!serversFor(groupId)) return fail('GROUP_NOT_FOUND', '群组不存在');
    return ok(settingsData(groupId)) as ApiEnvelope<T>;
  }

  if (method === 'POST' && endpoint === '/page/settings/preview') {
    const preview = previewSettings(body as SettingsMutationInput, true);
    if ('success' in preview) return preview as ApiEnvelope<T>;
    return ok(preview) as ApiEnvelope<T>;
  }

  if (method === 'POST' && endpoint === '/page/settings') {
    const input = body as SettingsMutationInput;
    const preview = previewSettings(input);
    if ('success' in preview) return preview as ApiEnvelope<T>;
    if (preview.requires_confirmation && !input.confirmation?.history_trim) {
      return fail('HISTORY_TRIM_CONFIRM_REQUIRED', '缩短历史保留上限需要明确确认') as ApiEnvelope<T>;
    }
    if (preview.history_trim.required) {
      const storedPreview = input.preview_id ? settingsPreviews.get(input.preview_id) : undefined;
      if (!storedPreview
        || storedPreview.signature !== settingsMutationSignature(input)
        || storedPreview.pointsToDelete !== preview.history_trim.points_to_delete
        || input.confirmation?.expected_points_to_delete !== preview.history_trim.points_to_delete) {
        return fail('SETTINGS_PREVIEW_STALE', '安全预览已失效，请重新预览') as ApiEnvelope<T>;
      }
    }

    const groupId = input.group_id ?? groups[0] ?? '';
    if (input.scope === 'global') {
      for (const [key, value] of Object.entries(input.values) as Array<[RuntimeSettingKey, RuntimeSettings[RuntimeSettingKey]]>) {
        globalSettings[key] = value as never;
      }
      globalRevision += 1;
    } else {
      const overrides = groupOverrides[groupId] ??= {};
      for (const key of input.reset_keys) delete overrides[key as GroupRuntimeSettingKey];
      for (const [key, value] of Object.entries(input.values) as Array<[GroupRuntimeSettingKey, RuntimeSettings[GroupRuntimeSettingKey]]>) {
        overrides[key] = value as never;
      }
      groupRevisions[groupId] = (groupRevisions[groupId] ?? 0) + 1;
    }

    let deletedPoints = 0;
    if (preview.history_trim.required) {
      const affected = preview.history_trim.affected_groups ?? [groupId];
      for (const affectedGroup of affected) {
        for (const points of Object.values(trendDb[affectedGroup] ?? {})) {
          const extra = Math.max(0, points.length - preview.history_trim.next_limit);
          if (extra) points.splice(0, extra);
          deletedPoints += extra;
        }
      }
    }
    if (input.preview_id) settingsPreviews.delete(input.preview_id);
    const result: SettingsSaveData = {
      effective: input.scope === 'global' ? clone(globalSettings) : effectiveSettings(groupId),
      revision: { global: globalRevision, group: groupRevisions[groupId] ?? 0 },
      history_trim: { performed: deletedPoints > 0, deleted_points: deletedPoints },
    };
    return ok(result) as ApiEnvelope<T>;
  }

  if (method === 'GET' && endpoint === '/page/servers') {
    const groupId = String(query?.group_id ?? '');
    const servers = serversFor(groupId);
    if (!servers) return fail('GROUP_NOT_FOUND', '群组不存在');
    return ok({ group_id: groupId, servers }) as ApiEnvelope<T>;
  }

  if (method === 'POST' && endpoint === '/page/servers/add') {
    const input = body as { group_id: string; name: string; host: string; force?: boolean };
    const servers = serversFor(input.group_id);
    if (!servers) return fail('GROUP_NOT_FOUND', '群组不存在');
    if (Object.values(servers).some((server) => server.name === input.name)) return fail('DUPLICATE_NAME', '已存在同名服务器');
    if (Object.values(servers).some((server) => server.host === input.host)) return fail('DUPLICATE_HOST', '已存在相同地址的服务器');
    if (!input.force && input.host.includes('offline')) return fail('PROBE_FAILED', '服务器预探测失败；确认地址后可使用 force=true 强制添加');
    const id = nextId(input.group_id);
    const server: SavedServer = {
      id,
      name: input.name,
      host: input.host,
      created_time: now(),
      last_success_time: now(),
      last_failed_time: null,
      failed_count: 0,
    };
    servers[String(id)] = server;
    return ok({ group_id: input.group_id, server }) as ApiEnvelope<T>;
  }

  if (method === 'POST' && endpoint === '/page/servers/update') {
    const input = body as { group_id: string; identifier: string; name?: string; host?: string };
    const found = findServer(input.group_id, input.identifier);
    if (!found) return fail('SERVER_NOT_FOUND', '服务器不存在');
    const [id, current] = found;
    const servers = serversFor(input.group_id)!;
    const name = input.name ?? current.name;
    const host = input.host ?? current.host;
    if (Object.entries(servers).some(([otherId, server]) => otherId !== id && server.name === name)) return fail('DUPLICATE_NAME', '已存在同名服务器');
    if (Object.entries(servers).some(([otherId, server]) => otherId !== id && server.host === host)) return fail('DUPLICATE_HOST', '已存在相同地址的服务器');
    const server = { ...current, name, host };
    servers[id] = server;
    return ok({ group_id: input.group_id, server }) as ApiEnvelope<T>;
  }

  if (method === 'POST' && endpoint === '/page/servers/delete') {
    const input = body as { group_id: string; identifier: string; confirm: boolean };
    if (input.confirm !== true) return fail('CONFIRM_REQUIRED', '删除服务器必须显式设置 confirm=true');
    const found = findServer(input.group_id, input.identifier);
    if (!found) return fail('SERVER_NOT_FOUND', '服务器不存在');
    const [id, server] = found;
    const trendExisted = Boolean(trendDb[input.group_id]?.[id]);
    delete serverDb[input.group_id][id];
    delete trendDb[input.group_id]?.[id];
    return ok({
      group_id: input.group_id,
      deleted: true,
      server,
      trend_cascade_deleted: true,
      trend_existed: trendExisted,
    }) as ApiEnvelope<T>;
  }

  if (method === 'POST' && endpoint === '/page/status') {
    const input = body as { group_id: string; identifier?: string };
    const servers = serversFor(input.group_id);
    if (!servers) return fail('GROUP_NOT_FOUND', '群组不存在');
    const selected = input.identifier
      ? [findServer(input.group_id, input.identifier)].filter((value): value is [string, SavedServer] => value !== null)
      : Object.entries(servers).sort(([left], [right]) => Number(left) - Number(right));
    if (input.identifier && !selected.length) return fail('SERVER_NOT_FOUND', '服务器不存在');
    const queriedAt = now();
    const results = selected.map(([id, server]) => {
      const online = !server.name.includes('红石');
      if (online) {
        server.last_success_time = queriedAt;
        server.failed_count = 0;
        const playersOnline = id === '1' ? 18 : 7;
        appendTrend(input.group_id, id, playersOnline);
        return {
          id: server.id,
          name: server.name,
          host: server.host,
          state: 'online',
          online: true,
          queried_at: queriedAt,
          latency: id === '1' ? 42 : 68,
          version: '1.21.4',
          players_online: playersOnline,
          players_max: 60,
          players_sample: ['Alex', 'Steve', 'Builder_01'],
          players_sample_complete: false,
          icon_base64: null,
        };
      }
      server.last_failed_time = queriedAt;
      server.failed_count += 1;
      return {
        id: server.id,
        name: server.name,
        host: server.host,
        state: 'unreachable',
        online: false,
        queried_at: queriedAt,
        latency: null,
        version: null,
        players_online: null,
        players_max: null,
        players_sample: [],
        players_sample_complete: false,
        icon_base64: null,
      };
    });
    return ok({ group_id: input.group_id, queried_at: queriedAt, servers: results }) as ApiEnvelope<T>;
  }

  if (method === 'GET' && endpoint === '/page/trends') {
    const groupId = String(query?.group_id ?? '');
    const servers = serversFor(groupId);
    if (!servers) return fail('GROUP_NOT_FOUND', '群组不存在');
    const hours = Number(query?.hours ?? 24);
    if (!Number.isInteger(hours) || hours < 1 || hours > 168) return fail('INVALID_HOURS', 'hours 必须在 1-168 之间');
    const selected = query?.identifier
      ? [findServer(groupId, query.identifier)].filter((value): value is [string, SavedServer] => value !== null)
      : Object.entries(servers).sort(([left], [right]) => Number(left) - Number(right));
    if (query?.identifier && !selected.length) return fail('SERVER_NOT_FOUND', '服务器不存在');
    const generatedAt = now();
    const currentBucket = Math.floor(generatedAt / 3600) * 3600;
    const cutoff = currentBucket - (hours - 1) * 3600;
    const results = selected.map(([id, server]) => {
      const points = (trendDb[groupId]?.[id] ?? []).filter((point) => cutoff <= point.ts && point.ts <= currentBucket);
      const counts = points.map((point) => point.count);
      return {
        server,
        points,
        latest: counts.at(-1) ?? null,
        max: counts.length ? Math.max(...counts) : null,
        average: counts.length ? Math.round(counts.reduce((sum, count) => sum + count, 0) / counts.length * 100) / 100 : null,
        count: counts.length,
      };
    });
    return ok({ group_id: groupId, hours, generated_at: generatedAt, servers: results }) as ApiEnvelope<T>;
  }

  if (method === 'GET' && endpoint === '/page/cleanup') {
    const groupId = String(query?.group_id ?? '');
    if (!serversFor(groupId)) return fail('GROUP_NOT_FOUND', '群组不存在');
    const cleanupDays = effectiveSettings(groupId).auto_cleanup_days;
    return ok({ group_id: groupId, cleanup_days: cleanupDays, candidates: cleanupCandidates(groupId, cleanupDays) }) as ApiEnvelope<T>;
  }

  if (method === 'POST' && endpoint === '/page/cleanup') {
    const input = body as { group_id: string; confirm: boolean };
    if (input.confirm !== true) return fail('CONFIRM_REQUIRED', '执行清理必须显式设置 confirm=true');
    if (!serversFor(input.group_id)) return fail('GROUP_NOT_FOUND', '群组不存在');
    const cleanupDays = effectiveSettings(input.group_id).auto_cleanup_days;
    const deleted = cleanupCandidates(input.group_id, cleanupDays);
    deleted.forEach((candidate) => {
      delete serverDb[input.group_id][candidate.id];
      delete trendDb[input.group_id]?.[candidate.id];
    });
    return ok({
      group_id: input.group_id,
      cleanup_days: cleanupDays,
      deleted,
      deleted_count: deleted.length,
    }) as ApiEnvelope<T>;
  }

  return fail('ENDPOINT_NOT_FOUND', `Mock 未实现 endpoint：${endpoint}`);
}
