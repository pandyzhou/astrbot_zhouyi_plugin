export type UnixTimestamp = number;
export type ServerStatus = 'unknown' | 'online' | 'offline';

export interface ApiErrorBody {
  code: string;
  message: string;
  details?: unknown;
}

export type ApiEnvelope<T> =
  | { success: true; data: T }
  | { success: false; error: ApiErrorBody };

export interface BackendSuccessEnvelope<T> {
  status: 'ok';
  data: T;
}

export interface BackendErrorEnvelope {
  status: 'error';
  message: string;
  data: {
    code: string;
    [key: string]: unknown;
  };
}

export type BackendEnvelope<T> = BackendSuccessEnvelope<T> | BackendErrorEnvelope;

export interface CapabilityStatus {
  available: boolean;
  enabled: boolean;
  initialized: boolean;
  error: string | null;
  reason?: string | null;
}

export interface BackendBootstrapData {
  brand?: string;
  api_version?: string;
  groups: Array<{ id: string }>;
  selected_group_id: string | null;
  capabilities?: {
    mc: CapabilityStatus;
    memory: CapabilityStatus;
  };
}

export interface BackendSavedServer {
  id: string | number;
  name: string;
  host: string;
  created_time: UnixTimestamp;
  last_success_time: UnixTimestamp | null;
  last_failed_time: UnixTimestamp | null;
  failed_count: number;
}

export interface BackendServersData {
  group_id: string;
  servers: Record<string, BackendSavedServer>;
}

export interface BackendServerMutationData {
  group_id: string;
  server: BackendSavedServer;
}

export interface BackendDeleteServerData {
  group_id: string;
  deleted: true;
  server: BackendSavedServer;
  trend_cascade_deleted: true;
  trend_existed: boolean;
}

export interface BackendStatusServer {
  id: string | number;
  name: string;
  host: string;
  state: 'online' | 'unreachable';
  online: boolean;
  queried_at: UnixTimestamp;
  latency: number | null;
  version: string | null;
  players_online: number | null;
  players_max: number | null;
  players_sample: string[];
  players_sample_complete: false;
  icon_base64: string | null;
}

export interface BackendStatusData {
  group_id: string;
  queried_at: UnixTimestamp;
  servers: BackendStatusServer[];
}

export interface BackendTrendPoint {
  ts: UnixTimestamp;
  count: number;
}

export interface BackendTrendServerResult {
  server: BackendSavedServer;
  points: BackendTrendPoint[];
  latest: number | null;
  max: number | null;
  average: number | null;
  count: number;
}

export interface BackendTrendsData {
  group_id: string;
  hours: number;
  generated_at: UnixTimestamp;
  servers: BackendTrendServerResult[];
}

export interface BackendCleanupPreviewData {
  group_id: string;
  cleanup_days: number;
  candidates: CleanupCandidate[];
}

export interface BackendCleanupExecuteData {
  group_id: string;
  cleanup_days: number;
  deleted: CleanupCandidate[];
  deleted_count: number;
}

export interface GroupOption {
  group_id: string;
  label: string;
}

export interface BootstrapData {
  brand: string;
  groups: GroupOption[];
  default_group_id: string | null;
  capabilities: {
    mc: CapabilityStatus;
    memory: CapabilityStatus;
  };
}

export interface PlayerSample {
  name: string;
  id?: string | null;
}

export interface ServerRecord {
  id: string;
  name: string;
  host: string;
  created_time: UnixTimestamp;
  last_success_time: UnixTimestamp | null;
  last_failed_time: UnixTimestamp | null;
  failed_count: number;
  status: ServerStatus;
  version: string | null;
  latency: number | null;
  players: {
    online: number;
    max: number;
    sample: PlayerSample[];
  } | null;
  icon: string | null;
  queried_at: UnixTimestamp | null;
}

export interface ServersData {
  group_id: string;
  servers: ServerRecord[];
  last_manual_refresh_time: UnixTimestamp | null;
}

export interface AddServerInput {
  group_id: string;
  name: string;
  host: string;
  force: boolean;
}

export interface UpdateServerInput {
  group_id: string;
  server_id: string;
  name: string;
  host: string;
}

export interface DeleteServerInput {
  group_id: string;
  server_id: string;
}

export interface ServerMutationData {
  server: ServerRecord;
}

export interface DeleteServerData {
  deleted_server_id: string;
  trend_cascade_deleted: boolean;
  trend_existed: boolean;
}

export interface RefreshStatusInput {
  group_id: string;
  server_id?: string;
}

export interface RefreshStatusData {
  group_id: string;
  refreshed_at: UnixTimestamp;
  servers: ServerRecord[];
}

export interface TrendPoint {
  timestamp: UnixTimestamp;
  players: number | null;
}

export interface TrendServerResult {
  server: Pick<ServerRecord, 'id' | 'name' | 'host'>;
  latest: number | null;
  max: number | null;
  average: number | null;
  count: number;
  points: TrendPoint[];
}

export interface TrendsData {
  group_id: string;
  hours: number;
  results: TrendServerResult[];
}

export interface CleanupCandidate {
  id: string;
  name: string;
  host: string;
  last_success_time: UnixTimestamp | null;
  effective_last_success_time: UnixTimestamp | null;
  failed_count: number;
}

export interface CleanupData {
  mode: 'preview' | 'execute';
  candidates: CleanupCandidate[];
  deleted_count: number;
}

export interface RuntimeSettings {
  max_history_points: number;
  trend_sampling_enabled: boolean;
  auto_cleanup_enabled: boolean;
  auto_cleanup_days: number;
  auto_refresh_on_page_open: boolean;
  default_trend_hours: number;
  mc_lookup_timeout_seconds: number;
  mc_status_timeout_seconds: number;
  max_concurrent_queries: number;
}

export type RuntimeSettingKey = keyof RuntimeSettings;
export type GroupRuntimeSettingKey = Exclude<RuntimeSettingKey, 'max_concurrent_queries'>;
export type SettingsScope = 'global' | 'group';

export interface SettingConstraint {
  min?: number;
  max?: number;
  step?: number;
  unit?: string;
}

export type SettingsConstraints = Partial<Record<RuntimeSettingKey, SettingConstraint>>;

export interface SettingsRevision {
  global: number;
  group: number;
}

export interface SettingsData {
  group_id: string;
  global: RuntimeSettings;
  group_overrides: Partial<Pick<RuntimeSettings, GroupRuntimeSettingKey>>;
  effective: RuntimeSettings;
  revision: SettingsRevision;
  constraints: SettingsConstraints;
}

export interface SettingsMutationInput {
  scope: SettingsScope;
  group_id?: string;
  values: Partial<RuntimeSettings>;
  reset_keys: RuntimeSettingKey[];
  expected_revision: number;
  preview_id?: string;
  confirmation?: {
    history_trim: true;
    expected_points_to_delete: number;
  };
}

export interface HistoryTrimImpact {
  required: boolean;
  current_limit: number;
  next_limit: number;
  affected_groups?: string[];
  affected_servers: number;
  points_to_delete: number;
}

export interface CleanupImpact {
  current_candidate_count: number;
  next_candidate_count: number;
  new_candidate_count: number;
}

export interface SettingsPreviewData {
  preview_id: string;
  current_effective: RuntimeSettings;
  next_effective: RuntimeSettings;
  requires_confirmation: boolean;
  history_trim: HistoryTrimImpact;
  cleanup_impact: CleanupImpact;
  revision: SettingsRevision;
}

export interface SettingsSaveData {
  effective: RuntimeSettings;
  revision: SettingsRevision;
  history_trim: {
    performed: boolean;
    deleted_points: number;
  };
}

export type SourceUpdateStatus = 'current' | 'new_version' | 'new_commits' | 'changed' | 'unavailable';

export interface SourceUpdateBaseline {
  version: string | null;
  commit_sha: string | null;
  repository: string;
  branch: string;
}

export interface SourceUpdateUpstream {
  version: string | null;
  commit_sha: string | null;
  committed_at: UnixTimestamp | null;
  commit_title: string | null;
  repository_url: string | null;
  commit_url: string | null;
}

export interface SourceUpdateItem {
  id: string;
  display_name: string;
  role: string;
  status: SourceUpdateStatus;
  stale: boolean;
  baseline: SourceUpdateBaseline;
  upstream: SourceUpdateUpstream;
  error: string | null;
}

export interface SourceUpdateRateLimit {
  limit: number | null;
  remaining: number | null;
  reset_at: UnixTimestamp | null;
}

export interface SourceUpdatesData {
  checked_at: UnixTimestamp | null;
  next_check_at: UnixTimestamp | null;
  refresh_allowed_at: UnixTimestamp | null;
  rate_limit: SourceUpdateRateLimit | null;
  sources: SourceUpdateItem[];
}
