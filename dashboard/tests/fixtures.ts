export const node = (id: number, tier: number, name: string, status: string, children: any[] = [],
                     extra: Record<string, unknown> = {}) => ({
  id, tier, name, status, current_task: null, last_run: null, cost_today: 0, children, ...extra,
});

export const TREE = {
  last_event_id: 5,
  tree: node(1, 1, "Master Orchestrator", "running", [
    node(2, 2, "Information Technology", "running", [
      node(3, 3, "Software", "running", [
        node(4, 4, "IT Services", "running", [
          node(5, 5, "Alpha Ltd", "running", [
            node(6, 6, "fundamentals", "running", [], { current_task: "reading filings" }),
          ]),
        ]),
      ]),
    ], { current_task: "sector sweep", cost_today: 2.5, last_run: "2026-11-06T04:00:00Z" }),
    node(20, 2, "Financials", "failed", [
      node(21, 3, "Banks", "failed", [node(22, 4, "Private Banks", "failed")]),
    ], { cost_today: 0.75 }),
    node(30, 2, "Healthcare", "idle", []),
  ]),
};

export const SUMMARY = {
  queue_depth: 3, compute_tokens_today: 12345, compute_model_seconds_today: 67.5,
  failures_today: 2, last_event_id: 5,
};

const fresh = (age: number, stale = false) => ({ as_of: "2026-11-06", age_days: age, stale });

export const SIGNALS = {
  count: 3, stale_threshold_days: 7, stale_threshold_status: "PLACEHOLDER",
  label: "research signal — not an order or investment advice",
  items: [
    { isin: "INE000A01011", name: "Alpha Ltd", sector: "Software", industry: "IT Services",
      rating: "accumulate", reasons: ["growth sub-score above threshold", "no red flags"],
      confidence: "high", rules_version: "rules-v1", ...fresh(0),
      label: "research signal — not an order or investment advice" },
    { isin: "INE000B01012", name: "Beta Ltd", sector: "Software", industry: "IT Services",
      rating: "hold", reasons: ["mixed sub-scores"], confidence: "medium", rules_version: "rules-v1",
      as_of: "2026-10-01", age_days: 36, stale: true,
      label: "research signal — not an order or investment advice" },
    { isin: "INE000C01013", name: "Gamma Ltd", sector: "Hardware", industry: "Devices",
      rating: "insufficient evidence", reasons: ["fewer than 3 sourced facts"], confidence: "low",
      rules_version: "rules-v2", ...fresh(2),
      label: "research signal — not an order or investment advice" },
  ],
};

export const ANALYST = {
  agent_id: 5, role: "company", name: "Alpha Ltd", isin: "INE000A01011",
  findings: { scorecard_id: 11, long_term_score: 72.5, short_term_score: 55, confidence: "high",
    sub_scores: { growth: 70, quality: 81 }, benefits: ["Strong cash conversion"],
    risks: ["Customer concentration"], red_flags: ["Auditor resigned in FY25"] },
  confidence: "high", rules_version: "rules-v1",
  freshness: fresh(1),
  evidence: [
    { claim_type: "fact", claim_ref: "revenue", chunk_id: null,
      fact: { id: 1, metric: "revenue", value: 1200.5, unit: "INR cr", period: "FY25",
              as_of: "2025-03-31", conflict: false },
      source: { id: 1, url: "https://www.bseindia.com/filing/alpha-fy25.pdf", type: "filing", tier: 1,
                publish_date: "2025-05-10", fetched_at: "2026-11-05T10:00:00Z" } },
    { claim_type: "chunk", claim_ref: "risk-1", chunk_id: 7, fact: null,
      source: { id: 2, url: "http://example.org/news/alpha-contract", type: "news", tier: 3,
                publish_date: "2026-09-01", fetched_at: null } },
    { claim_type: "fact", claim_ref: "margin", chunk_id: null, fact: null,
      source: { id: 3, url: "javascript:alert(1)", type: "web", tier: 4,
                publish_date: null, fetched_at: null } },
  ],
  run_history: [
    { run_log_id: 9, run_id: "run-2026-11-05", role: "fundamentals", model_version: "qwen-x",
      status: "completed", created_at: "2026-11-05T10:00:00Z", prompt_tokens: 100,
      completion_tokens: 50, total_tokens: 150, model_seconds: 3.2 },
  ],
  rule_inputs: { rules_version: "rules-v1", scorecard_run_date: "2026-11-05",
    scorecard_inputs: { sub_scores: { growth: 70 }, long_term_score: 72.5 },
    latest_run_inputs: { ticker: "ALPHA", window: "FY25" } },
};

export const SECTORS = {
  count: 2, stale_threshold_days: 7, stale_threshold_status: "PLACEHOLDER",
  label: "research signal — not an order or investment advice",
  items: [
    { sector: "Software", industries: ["IT Services"], stock_count: 2, scored_count: 2,
      avg_long_term_score: 65.25, avg_short_term_score: 50, confidence: "medium",
      confidence_counts: { high: 1, medium: 1 }, signal_counts: { accumulate: 1, hold: 1 },
      rules_version: "rules-v1", rules_versions: ["rules-v1"], ...fresh(0),
      label: "research signal — not an order or investment advice" },
    { sector: "Hardware", industries: ["Devices"], stock_count: 1, scored_count: 0,
      avg_long_term_score: null, avg_short_term_score: null, confidence: null,
      confidence_counts: {}, signal_counts: {}, rules_version: null, rules_versions: [],
      as_of: null, age_days: null, stale: true,
      label: "research signal — not an order or investment advice" },
  ],
};

export const STOCKS = {
  count: 2, stale_threshold_days: 7, stale_threshold_status: "PLACEHOLDER",
  items: [
    { isin: "INE000A01011", name: "Alpha Ltd", nse_code: "ALPHA", bse_code: "500001",
      sector: "Software", industry: "IT Services", long_term_score: 72.5, short_term_score: 55,
      confidence: "high", rules_version: "rules-v1", ...fresh(0), rating: "accumulate",
      label: "research signal — not an order or investment advice" },
    { isin: "INE000B01012", name: "Beta Ltd", nse_code: null, bse_code: null,
      sector: "Software", industry: "IT Services", long_term_score: null, short_term_score: null,
      confidence: null, rules_version: null, as_of: null, age_days: null, stale: true, rating: null,
      label: "research signal — not an order or investment advice" },
  ],
};

export const STOCK = {
  ...STOCKS.items[0], agent_id: 5,
  sub_scores: { growth: 70, quality: 81 }, benefits: ["Strong cash conversion"],
  risks: ["Customer concentration"], red_flags: ["Auditor resigned in FY25"],
  signal: SIGNALS.items[0],
};

export const HISTORY = {
  isin: "INE000A01011", count: 2, stale_threshold_days: 7, stale_threshold_status: "PLACEHOLDER",
  label: "research signal — not an order or investment advice",
  items: [
    { run_date: "2026-11-01", long_term_score: 60, short_term_score: null, confidence: "medium", rating: "hold" },
    { run_date: "2026-11-06", long_term_score: 72.5, short_term_score: 55, confidence: "high", rating: "accumulate" },
  ],
};
