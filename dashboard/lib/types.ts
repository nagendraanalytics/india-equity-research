// Mirrors src/ier/api/schemas.py. All text from the API is untrusted: render as text only.
/** Status values of GET /agents/tree (AgentNodeOut.status). "blocked" = queued but waiting for
 *  an unfinished parent job (e.g. a company job waiting for its sector/industry job). */
export type AgentStatus =
  "running" | "failed" | "degraded" | "blocked" | "queued" | "completed" | "idle";
export interface TreeNode {
  id: number;
  tier: number;
  name: string;
  status: AgentStatus | string;
  current_task: string | null;
  last_run: string | null;
  cost_today: number;
  children: TreeNode[];
}
export interface TreeResponse { last_event_id: number; tree: TreeNode }
export interface Summary {
  queue_depth: number;
  compute_tokens_today: number;
  compute_model_seconds_today: number;
  failures_today: number;
  last_event_id: number;
}
export interface AgentEvent {
  id: number;
  agent_id: number;
  kind: string;
  payload: unknown;
  cost: number;
  created_at: string | null;
}
export interface RerunResponse { job_id: number; agent_id: number; already_queued: boolean; event_id: number | null }

export interface Fresh { as_of: string | null; age_days: number | null; stale: boolean }
export interface ListMeta { count: number; stale_threshold_days: number; stale_threshold_status: string }

export interface SignalItem extends Fresh {
  isin: string; name: string; sector: string | null; industry: string | null;
  rating: string; reasons: string[]; confidence: string | null; rules_version: string; label: string;
}
export interface SignalsResponse extends ListMeta { items: SignalItem[]; label: string }

export interface StockItem extends Fresh {
  isin: string; name: string; nse_code: string | null; bse_code: string | null;
  sector: string | null; industry: string | null;
  long_term_score: number | null; short_term_score: number | null;
  confidence: string | null; rules_version: string | null; rating: string | null; label: string;
}
export interface StocksResponse extends ListMeta { items: StockItem[] }
export interface StockDetail extends StockItem {
  agent_id: number | null; sub_scores: unknown; benefits: unknown; risks: unknown; red_flags: unknown;
  signal: SignalItem | null;
}

export interface SectorItem extends Fresh {
  sector: string; industries: string[]; stock_count: number; scored_count: number;
  avg_long_term_score: number | null; avg_short_term_score: number | null;
  confidence: string | null; confidence_counts: Record<string, number>;
  signal_counts: Record<string, number>; rules_version: string | null; rules_versions: string[]; label: string;
}
export interface SectorsResponse extends ListMeta { items: SectorItem[]; label: string }

export interface Evidence {
  claim_type: string; claim_ref: string | null; chunk_id: number | null;
  fact: { id: number; metric: string; value: number; unit: string | null; period: string;
          as_of: string | null; conflict: boolean } | null;
  source: { id: number; url: string; type: string; tier: number; publish_date: string | null;
            fetched_at: string | null } | null;
}
export interface RunHistoryItem {
  run_log_id: number; run_id: string | null; role: string | null; model_version: string | null;
  status: string | null; created_at: string | null; prompt_tokens: number; completion_tokens: number;
  total_tokens: number; model_seconds: number;
}
export interface Analyst {
  agent_id: number; role: string; name: string; isin: string | null;
  findings: { scorecard_id: number; long_term_score: number | null; short_term_score: number | null;
              confidence: string | null; sub_scores: unknown; benefits: unknown; risks: unknown;
              red_flags: unknown } | null;
  confidence: string | null; rules_version: string | null; freshness: Fresh;
  evidence: Evidence[]; run_history: RunHistoryItem[]; rule_inputs: unknown;
}

export interface HistoryPoint {
  run_date: string; long_term_score: number | null; short_term_score: number | null;
  confidence: string | null; rating: string | null;
}
export interface HistoryResponse extends ListMeta { isin: string; items: HistoryPoint[]; label: string }

export interface MinutePoint { t: string; started: number; completed: number; failed: number }
export interface HourPoint { t: string; tokens: number; model_seconds: number }
export interface RecentFailure { event_id: number; agent_id: number; created_at: string | null; error: string | null }
export interface MetricsResponse {
  events_by_minute: MinutePoint[]; tokens_by_hour: HourPoint[]; jobs_by_status: Record<string, number>;
  recent_failures: RecentFailure[]; last_event_id: number;
}

export interface AgentDetail {
  id: number; tier: number; tier_name: string; role: string; name: string; parent_id: number | null;
  scope_sector: string | null; isin: string | null; status: string; current_task: string | null;
  last_run: string | null; rerunnable: boolean;
  cost: { cost_today: number; event_cost_total: number; total_tokens: number; model_seconds: number };
  last_error: { message: string | null } | null;
  last_output: { event_id: number; created_at: string | null; payload: unknown } | null;
}

export type SourceItem = {
  id: string; segment: string; name: string; kind: string; terms: string | null;
  refresh_minutes: number; status: string; detail: string | null;
  last_checked_at: string | null; last_ok_at: string | null; count: number | null; latest: unknown;
};
export type SourceSegment = {
  id: string; name: string; note: string | null; connected: number; total: number; sources: SourceItem[];
};
export type SourcesResponse = {
  segments: SourceSegment[]; connected: number; total: number; affects_score: boolean; note: string;
};

// --- algorithm library ---
export type ChartSeries = { name: string; values: Array<number | null>; style: "line" | "dashed" | "bar" | "band_lo" | "band_hi" };
export type ChartBand = { lo: number; hi: number; label: string };
export type ChartPanel = { title: string; kind: "price" | "line" | "bars"; series: ChartSeries[]; y_bands: ChartBand[] };
export type ChartSpec = { dates: string[]; panels: ChartPanel[]; markers: Array<{ day: string; label: string }>; caption: string; as_of: string | null };

export type AlgoInfo = {
  id: string; name: string; family: string; horizon: "long" | "short"; min_history: number; description: string;
  band: number[] | null; has_chart: boolean;
};
export type AlgoCatalogue = { items: AlgoInfo[]; count: number; blend_weight: number; max_blend_weight: number; notes: string[]; macro_sectors: string[] };
export type AlgoComposite = { long: number | null; short: number | null; confidence: number; agreement: number | null; n_available: number; n_total: number };
export type AlgoScoreItem = { algo_id: string; name: string; family: string; horizon: "long" | "short"; score: number | null; value: number | null; reason: string; as_of: string };
export type StockAlgos = {
  isin: string; as_of: string | null; age_days: number | null; stale: boolean; composite: AlgoComposite | null;
  rules: { long: number | null; short: number | null };
  with_algorithms: { long: number | null; short: number | null; weight: number | null };
  items: AlgoScoreItem[]; label: string;
};
export type SectorAlgoItem = { isin: string; name: string; as_of: string | null; composite: AlgoComposite | null; family_scores: Record<string, number | null> };
export type SectorAlgos = { macro_sector: string; items: SectorAlgoItem[]; count: number; label: string };
export type UniverseItem = { isin: string; name: string; macro_sector: string | null; score: number | null; value: number | null; reason: string; as_of: string };
export type AlgoUniverse = { algo: AlgoInfo; items: UniverseItem[]; count: number; label: string };
