import type {
  ApiEnvelope,
  CleanupCandidate,
  GroupRuntimeSettingKey,
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
let sourceUpdatesCheckedAt = now() - 120;

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
    const timer = window.setTimeout(finish, 120);
    const onAbort = () => {
      window.clearTimeout(timer);
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

export async function mockRequest<T>(
  endpoint: string,
  method: 'GET' | 'POST',
  query: Query | undefined,
  body: unknown,
  signal?: AbortSignal,
): Promise<ApiEnvelope<T>> {
  await wait(signal);

  if (method === 'GET' && endpoint === '/page/v1/sources/updates') {
    return ok(sourceUpdatesData()) as ApiEnvelope<T>;
  }

  if (method === 'POST' && endpoint === '/page/v1/sources/updates/refresh') {
    return ok(sourceUpdatesData(true)) as ApiEnvelope<T>;
  }

  if (method === 'GET' && (endpoint === '/page/bootstrap' || endpoint === '/page/v1/bootstrap')) {
    return ok({ groups: groups.map((id) => ({ id })), selected_group_id: groups[0] ?? null }) as ApiEnvelope<T>;
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
