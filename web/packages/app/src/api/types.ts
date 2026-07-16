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

export type MemoryConfigValue = null | boolean | number | string | MemoryConfigValue[] | { [key: string]: MemoryConfigValue };
export type MemoryConfigObject = { [key: string]: MemoryConfigValue };
export type MemoryConfigRevision = string;
export type MemoryConfigReloadStatus = 'idle' | 'scheduled' | 'running' | 'failed';

export interface MemoryConfigSchemaNode {
  type?: 'object' | 'boolean' | 'bool' | 'string' | 'integer' | 'number' | 'int' | 'float';
  title?: string;
  label?: string;
  description?: string;
  hint?: string;
  default?: MemoryConfigValue;
  options?: unknown[] | Record<string, unknown>;
  enum?: unknown[];
  properties?: Record<string, MemoryConfigSchemaNode>;
  items?: Record<string, MemoryConfigSchemaNode>;
  _special?: string;
  minimum?: number;
  maximum?: number;
  min?: number;
  max?: number;
  step?: number;
  unit?: string;
  provider_type?: 'llm' | 'embedding';
  [key: string]: unknown;
}

export interface MemoryProviderOption {
  id: string;
  label?: string;
  model?: string;
  type?: string;
}

export interface MemoryProviderOptions {
  llm: MemoryProviderOption[];
  embedding: MemoryProviderOption[];
}

export interface MemoryFieldConstraint {
  min?: number;
  max?: number;
  exclusive_min?: number;
  exclusive_max?: number;
  step?: number;
  unit?: string;
  required?: boolean;
  pattern?: string;
}

export interface MemoryConfigData {
  schema: MemoryConfigSchemaNode | Record<string, MemoryConfigSchemaNode>;
  config: MemoryConfigObject;
  values?: MemoryConfigObject;
  revision: MemoryConfigRevision;
  runtime_id: string;
  runtime_generation?: number;
  reload_status?: MemoryConfigReloadStatus;
  reload_failed?: boolean;
  providers: MemoryProviderOptions;
  constraints: Record<string, MemoryFieldConstraint | Record<string, unknown>>;
}

export interface MemoryConfigMutationInput {
  config: MemoryConfigObject;
  expected_revision: MemoryConfigRevision;
}

export interface MemoryConfigSaveData {
  config: MemoryConfigObject;
  revision: MemoryConfigRevision;
  old_runtime_id: string;
  runtime_id?: string;
  reload_scheduled: boolean;
  reload_pending: boolean;
  reload_status?: MemoryConfigReloadStatus;
  reload_failed?: boolean;
  manual_reload_required: boolean;
  message?: string;
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

export type MemoryObjectScope = 'user' | 'persona' | 'session' | 'public' | 'legacy_session';
export type MemoryObjectStatus = 'active' | 'conflicted' | 'archived' | 'superseded';
export type MemoryIndexStatus = 'synced' | 'pending' | 'needs_repair' | 'disabled';
export type MemoryConflictStatus = 'open' | 'resolved' | 'dismissed';
export type MemoryRelationType = 'merged_into' | 'supersedes' | 'derived_from' | 'duplicate_of' | 'conflicts_with' | 'related_to';

export interface MemoryObject {
  memory_item_id: string;
  owner_user_id: string;
  owner_display_name: string | null;
  scope: MemoryObjectScope;
  session_id: string | null;
  persona_id: string | null;
  memory_type: string;
  canonical_key: string | null;
  status: MemoryObjectStatus;
  content: string;
  structured_payload: Record<string, unknown> | null;
  current_revision_no: number;
  version: number;
  importance: number;
  confidence: number;
  useful_score: number;
  group_safe: boolean;
  current_document_id: number | null;
  index_status: MemoryIndexStatus;
  conflict_count: number;
  source_count: number;
  relation_count: number;
  created_at: UnixTimestamp | null;
  updated_at: UnixTimestamp | null;
}

export interface MemoryObjectFilters {
  page: number;
  page_size: number;
  owner_user_id: string;
  keyword?: string;
  scope?: MemoryObjectScope | 'all';
  persona_id?: string;
  status?: MemoryObjectStatus | 'all';
  memory_type?: string;
  conflict?: 'all' | 'yes' | 'no';
  index_status?: MemoryIndexStatus | 'all';
  sort?: string;
}

export interface MemoryObjectsData {
  items: MemoryObject[];
  total: number;
  page: number;
  page_size: number;
  has_more: boolean;
}

export interface MemoryRevision {
  memory_item_id: string;
  revision_no: number;
  operation: 'create' | 'update' | 'merge' | 'supersede' | 'archive' | string;
  content: string;
  structured_payload: Record<string, unknown> | null;
  base_version: number | null;
  actor: string | null;
  reason: string | null;
  created_at: UnixTimestamp | null;
}

export interface MemorySourceMessage {
  source_id: string;
  revision_no: number;
  source_type: string;
  document_id: number | null;
  message_id_start: string | null;
  message_id_end: string | null;
  session_id: string | null;
  platform_id: string | null;
  content_snapshot: string | null;
  availability: 'available' | 'partial' | 'unavailable' | string;
  created_at: UnixTimestamp | null;
}

export interface MemoryRelation {
  relation_id: string;
  relation_type: MemoryRelationType;
  source_memory_item_id: string;
  target_memory_item_id: string;
  target_content: string | null;
  created_at: UnixTimestamp | null;
}

export interface MemoryConflict {
  conflict_id: string;
  owner_user_id: string;
  conflict_type: string;
  severity: 'low' | 'medium' | 'high' | string;
  status: MemoryConflictStatus;
  left_item: MemoryObject;
  right_item: MemoryObject;
  resolution: string | null;
  resolution_reason: string | null;
  created_at: UnixTimestamp | null;
  resolved_at: UnixTimestamp | null;
}

export interface MemoryObjectDetailData {
  item: MemoryObject;
  relations: MemoryRelation[];
  conflicts: MemoryConflict[];
}

export interface MemoryObjectMutationInput {
  owner_user_id: string;
  expected_version: 0;
  scope: MemoryObjectScope;
  content: string;
  persona_id?: string | null;
  session_id?: string | null;
  memory_type: string;
  canonical_key?: string | null;
  structured_payload?: Record<string, unknown> | null;
  importance?: number;
  confidence?: number;
  group_safe?: boolean;
  reason?: string;
}

export interface MemoryObjectUpdateInput extends Partial<Omit<MemoryObjectMutationInput, 'owner_user_id' | 'expected_version'>> {
  owner_user_id: string;
  memory_item_id: string;
  expected_version: number;
}

export interface MemoryObjectVersionInput {
  memory_item_id: string;
  expected_version: number;
}

export interface MemoryObjectBatchInput {
  owner_user_id: string;
  action: 'archive' | 'index_retry';
  items: MemoryObjectVersionInput[];
}

export interface MemoryIndexRetryInput {
  owner_user_id: string;
  items: MemoryObjectVersionInput[];
}

export interface MemorySupersedeInput {
  owner_user_id: string;
  old_memory_item_id: string;
  new_memory_item_id: string;
  expected_versions: Record<string, number>;
  reason?: string;
}

export interface MemoryMergePreviewInput {
  owner_user_id: string;
  survivor_memory_item_id: string;
  source_memory_item_ids: string[];
  expected_versions: Record<string, number>;
}

export interface MemoryMergePreviewData {
  owner_user_id: string;
  survivor_memory_item_id: string;
  source_memory_item_ids: string[];
  merged_content: string;
  merged_structured_payload: Record<string, unknown> | null;
  warnings: string[];
  expected_versions: Record<string, number>;
}

export interface MemoryMergeInput extends MemoryMergePreviewInput {
  content: string;
  structured_payload?: Record<string, unknown> | null;
  reason?: string;
}

export interface MemoryConflictResolveInput {
  owner_user_id: string;
  conflict_id: string;
  action: 'merge' | 'supersede_left' | 'supersede_right' | 'dismiss';
  expected_versions: Record<string, number>;
  survivor_memory_item_id?: string;
  content?: string;
  reason?: string;
}

export interface MemoryIdentityAlias {
  identity_link_id: string;
  owner_user_id: string;
  platform_id: string;
  bot_id: string;
  external_user_id: string;
  verified: boolean;
  source: string;
  status: string;
  created_at: UnixTimestamp | null;
  updated_at: UnixTimestamp | null;
}

export type MemoryOwnerStatus = 'active' | 'merged' | 'disabled';

export interface MemoryOwner {
  owner_user_id: string;
  display_name: string;
  status: MemoryOwnerStatus;
  aliases: MemoryIdentityAlias[];
  created_at: UnixTimestamp | null;
  updated_at: UnixTimestamp | null;
  expected_updated_at: string;
}

export interface MemoryIdentitiesData {
  owners: MemoryOwner[];
  unmapped_aliases: MemoryIdentityAlias[];
  total: number;
}

export interface MemoryOwnerUpdateInput {
  owner_user_id: string;
  display_name: string;
  status: MemoryOwnerStatus;
  expected_updated_at: string;
}

export interface MemoryAliasMoveInput {
  identity_link_id: string;
  owner_user_id: string;
  expected_owner_user_id: string;
}

export interface MemoryOwnerMergeState {
  status: string;
  updated_at: string;
  alias_count: string;
  alias_updated_at: string;
  memory_item_count: string;
  memory_item_updated_at: string;
  memory_version_sum: string;
  conflict_count: string;
  conflict_updated_at: string;
  [key: string]: string;
}

export interface MemoryOwnerMergePreviewData {
  preview_id: string;
  survivor_owner_user_id: string;
  source_owner_user_ids: string[];
  alias_count: number;
  memory_item_count: number;
  conflict_count: number;
  warnings: string[];
  expected_owner_states: Record<string, MemoryOwnerMergeState>;
}

export interface MemoryOwnerMergeInput {
  survivor_owner_user_id: string;
  source_owner_user_ids: string[];
  preview_id: string;
  expected_owner_states: Record<string, MemoryOwnerMergeState>;
}

export interface MemoryMigrationStatus {
  state: 'idle' | 'running' | 'completed' | 'failed' | string;
  processed: number;
  total: number;
  created: number;
  deduped: number;
  skipped: number;
  conflicted: number;
  errors: number;
  unresolved_owner_count: number;
}

export interface MemoryIndexMaintenanceStatus {
  state: 'synced' | 'degraded' | 'repairing' | string;
  synced_count: number;
  pending_count: number;
  needs_repair_count: number;
  disabled_count: number;
  last_success_at: UnixTimestamp | null;
  last_error: string | null;
}

export interface MemorySourceCoverageStatus {
  total_items: number;
  covered_items: number;
  partial_items: number;
  unavailable_items: number;
  coverage_ratio: number;
}

export interface MemoryMaintenanceStatusData {
  migration: MemoryMigrationStatus;
  index: MemoryIndexMaintenanceStatus;
  sources: MemorySourceCoverageStatus;
}
