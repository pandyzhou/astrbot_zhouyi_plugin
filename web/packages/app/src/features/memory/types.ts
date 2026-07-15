export type JsonValue = string | number | boolean | null | JsonValue[] | { [key: string]: JsonValue };

export interface MemoryUpdateHistoryItem {
  timestamp?: string | number;
  field?: string;
  old_value?: unknown;
  new_value?: unknown;
  reason?: string;
  description?: string;
  [key: string]: unknown;
}

export interface MemoryMetadata {
  memory_type?: string;
  importance?: number;
  status?: string;
  session_id?: string | null;
  persona_id?: string | null;
  create_time?: number | string;
  updated_at?: number | string;
  last_access_time?: number | string;
  key_facts?: string[];
  topics?: string[];
  update_history?: MemoryUpdateHistoryItem[];
  [key: string]: unknown;
}

export interface MemoryItem {
  id: number;
  doc_id?: string;
  text: string;
  metadata: MemoryMetadata;
  created_at?: string | number;
  updated_at?: string | number;
}

export interface MemoryListData {
  items: MemoryItem[];
  total: number;
  page: number;
  page_size: number;
  has_more: boolean;
}

export interface GraphNode {
  id: number;
  key?: string;
  label?: string;
  canonical_value?: string;
  type?: string;
  metadata?: Record<string, unknown>;
  entry_count?: number;
  memory_count?: number;
  degree?: number;
  weight?: number;
  highlighted?: boolean;
}

export interface GraphEdge {
  id?: number;
  key?: string;
  source: number;
  target: number;
  relation_type?: string;
  memory_id?: number;
  weight?: number;
  confidence?: number;
  status?: string;
  metadata?: Record<string, unknown>;
}

export interface GraphEntry {
  id: number;
  memory_id: number;
  entry_type?: string;
  relation_type?: string;
  content?: string;
  metadata?: Record<string, unknown>;
  session_id?: string | null;
  persona_id?: string | null;
  edge_id?: number | null;
  node_ids: number[];
}

export interface GraphRetrievalItem {
  memory_id: number;
  content?: string;
  metadata?: MemoryMetadata;
  final_score?: number;
  rrf_score?: number | null;
  bm25_score?: number | null;
  vector_score?: number | null;
  score_breakdown?: Record<string, number>;
  source?: string;
  entry_id?: number;
  matched_node_ids?: number[];
}

export interface GraphMemory {
  memory_id: number;
  summary?: string;
  content?: string;
  session_id?: string | null;
  persona_id?: string | null;
  memory_type?: string;
  importance?: number;
  entry_count?: number;
  edge_count?: number;
  node_count?: number;
  entry_types?: string[];
  retrieval?: GraphRetrievalItem;
}

export interface GraphSummary {
  visible_node_count: number;
  visible_edge_count: number;
  visible_entry_count: number;
  visible_memory_count: number;
  graph_node_count: number;
  graph_edge_count: number;
  graph_entry_count: number;
  graph_memory_enabled: boolean;
  node_type_breakdown: Record<string, number>;
  relation_breakdown: Record<string, number>;
}

export interface GraphPayload {
  enabled: boolean;
  mode: string;
  query?: string | null;
  memory_id?: number | null;
  filters?: { session_id?: string | null; persona_id?: string | null; [key: string]: unknown };
  summary: GraphSummary;
  matched_node_ids: number[];
  matched_memory_ids: number[];
  top_nodes: GraphNode[];
  top_memories: GraphMemory[];
  retrieval: { total: number; items: GraphRetrievalItem[] };
  snapshot: {
    nodes: GraphNode[];
    edges: GraphEdge[];
    memories: GraphMemory[];
    entries: GraphEntry[];
  };
}

export interface MemoryGraphContext {
  nodes: GraphNode[];
  edges: GraphEdge[];
  entries: GraphEntry[];
}

export interface MemoryDetail extends MemoryMetadata {
  memory_id: number;
  doc_id?: string;
  text: string;
  summary?: string;
  created_at?: string | number;
  updated_at?: string | number;
  metadata: MemoryMetadata;
  graph_context?: MemoryGraphContext | null;
}

export interface RecallSession {
  session_id: string;
  group_id: string;
  display_name?: string | null;
  message_count: number;
}

export interface StatsData {
  total_memories?: number;
  status_breakdown?: Record<string, number>;
  graph_nodes?: number;
  graph_edges?: number;
  graph_entries?: number;
  atom_count?: number;
  atom_breakdown?: Record<string, number>;
  importance_distribution?: Record<string, number>;
  recall_sessions?: RecallSession[];
  sessions?: Record<string, number>;
  recent_sessions?: Array<{ session_id: string; message_count: number; last_active?: string }>;
}

export interface BackupItem {
  name?: string;
  directory?: string;
  backup_timestamp?: string;
  file_count?: number;
  files_copied?: number;
}

export interface RecallItem {
  memory_id: number | string;
  content: string;
  similarity_score: number;
  score_percentage?: number;
  metadata: MemoryMetadata;
  score_breakdown?: Record<string, number>;
}

export interface RecallData {
  results: RecallItem[];
  total: number;
  query?: string;
  k?: number;
  session_id_filter?: string | null;
  elapsed_time_ms: number;
}
