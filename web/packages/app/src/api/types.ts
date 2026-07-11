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

export interface BackendBootstrapData {
  groups: Array<{ id: string }>;
  selected_group_id: string | null;
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
  groups: GroupOption[];
  default_group_id: string | null;
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
