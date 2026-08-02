/** Types mirroring the backend's Pydantic schemas. */

export type Role = 'user' | 'admin';
export type Tier = 'free' | 'pro';
export type Severity = 'critical' | 'high' | 'medium' | 'low';
export type Priority = 'urgent' | 'high' | 'medium' | 'low';
export type Verdict = 'approve' | 'comment' | 'request_changes';
export type TicketCategory =
  | 'billing'
  | 'bug'
  | 'account'
  | 'how_to'
  | 'feature_request'
  | 'general';

export interface User {
  id: string;
  email: string;
  full_name: string | null;
  role: Role;
  is_active: boolean;
  created_at: string;
}

export interface TokenPair {
  access_token: string;
  refresh_token: string;
  token_type: string;
  expires_in: number;
}

export interface AuthResponse {
  user: User;
  tokens: TokenPair;
}

export interface UsageStats {
  request_id: string;
  latency_ms: number;
  llm_calls: number;
  total_tokens: number;
  cost_usd: number;
  cache_hit: boolean;
}

/** A structured agent step, streamed live over the trace WebSocket. */
export interface TraceEvent {
  request_id: string;
  pod: string;
  node: string;
  phase: 'start' | 'finish' | 'error';
  sequence: number;
  duration_ms: number | null;
  detail: Record<string, unknown>;
  message: string;
  timestamp?: string;
}

export interface Citation {
  index: number;
  document_id: string;
  document_title: string | null;
  source: string | null;
  snippet: string;
}

export interface RetrievedChunk {
  chunk_id: string;
  document_id: string;
  text: string;
  source: string | null;
  document_title: string | null;
  score: number;
  vector_rank: number | null;
  keyword_rank: number | null;
  fused_score: number;
  rerank_score: number | null;
}

export interface GroundednessVerdict {
  grounded: boolean;
  score: number;
  reason: string;
  answer_relevance: number;
}

export interface ToolInvocation {
  tool: string;
  arguments: Record<string, unknown>;
  result: Record<string, unknown>;
  latency_ms: number;
}

export interface QueryResponse {
  request_id: string;
  question: string;
  answer: string;
  found: boolean;
  citations: Citation[];
  chunks: RetrievedChunk[];
  retrieval_loops: number;
  reformulated_queries: string[];
  groundedness: GroundednessVerdict | null;
  critique: string | null;
  tool_invocations: ToolInvocation[];
  trace: TraceEvent[];
  usage: UsageStats;
}

export interface IngestResponse {
  document_id: string;
  title: string;
  chunk_count: number;
  char_count: number;
  collection: string;
  reingested: boolean;
  cache_invalidated: number;
}

export interface DocumentSummary {
  id: string;
  title: string;
  source: string | null;
  collection: string;
  chunk_count: number;
  char_count: number;
  status: string;
  created_at: string;
}

export interface CodeIssue {
  severity: Severity;
  category: string;
  line: number | null;
  title: string;
  explanation: string;
  suggestion: string | null;
  agent: string | null;
}

export interface CodeReviewResult {
  request_id: string;
  review_id: string | null;
  filename: string | null;
  language: string;
  verdict: Verdict;
  summary: string;
  issues: CodeIssue[];
  issue_count: number;
  blocking_count: number;
  severity_counts: Record<string, number>;
  top_recommendation: string | null;
  trace: TraceEvent[];
  usage: UsageStats;
}

export interface KBSource {
  document_id: string;
  title: string | null;
  snippet: string;
  score: number;
}

export interface TriageResponse {
  request_id: string;
  ticket_id: string;
  subject: string;
  priority: Priority;
  category: TicketCategory;
  confidence: number;
  classification_path: 'trained_model' | 'llm_fallback';
  draft_response: string;
  escalate: boolean;
  escalation_reason: string | null;
  suggested_owner: string | null;
  kb_sources: KBSource[];
  trace: TraceEvent[];
  usage: UsageStats;
}

export interface PodStats {
  pod: string;
  requests: number;
  llm_calls: number;
  total_tokens: number;
  total_cost_usd: number;
  avg_latency_ms: number;
  p95_latency_ms: number;
  error_rate: number;
  cache_hits: number;
  cache_hit_rate: number;
  estimated_cost_saved_usd: number;
  avg_retrieval_loops: number | null;
  avg_faithfulness: number | null;
  avg_answer_relevance: number | null;
  trained_model_share: number | null;
}

export interface ObservabilitySummary {
  window_hours: number;
  generated_at: string;
  total_requests: number;
  total_llm_calls: number;
  total_tokens: number;
  total_cost_usd: number;
  estimated_cost_saved_usd: number;
  overall_cache_hit_rate: number;
  avg_latency_ms: number;
  p95_latency_ms: number;
  error_rate: number;
  avg_faithfulness: number | null;
  avg_answer_relevance: number | null;
  pods: PodStats[];
  top_operations: Array<{
    operation: string;
    calls: number;
    cost_usd: number;
    avg_latency_ms: number;
  }>;
}

export interface Subscription {
  tier: Tier;
  status: string;
  current_period_end: string | null;
  cancel_at_period_end: boolean;
  stripe_customer_id: string | null;
}

export interface UsageResponse {
  tier: Tier;
  used: number;
  limit: number;
  remaining: number;
  window_seconds: number;
  resets_in_seconds: number;
  unlimited: boolean;
}

export interface CheckoutResponse {
  checkout_url: string;
  session_id: string;
  tier: Tier;
  mode: 'stripe' | 'simulated';
}

export interface EscalationEvent {
  ticket_id: string;
  request_id: string;
  subject: string;
  priority: Priority;
  category: TicketCategory;
  reason: string;
  suggested_owner: string | null;
  customer_email: string | null;
  created_at: string;
}

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}
