import type { AlgoCatalogue, AlgoUniverse, ChartSpec, SectorAlgos, StockAlgos } from "../lib/types";

export const ISIN = "INE009A01021";

export const CATALOGUE: AlgoCatalogue = {
  count: 4, blend_weight: 0, max_blend_weight: 0.3, notes: [], macro_sectors: ["Healthcare", "Information Technology"],
  items: [
    { id: "mom_3m", name: "3-month return", family: "momentum", horizon: "short", min_history: 64, description: "Percent change in close over 63 trading days.", band: [-20, 20], has_chart: true },
    { id: "rsi14", name: "RSI (14)", family: "momentum", horizon: "short", min_history: 15, description: "Relative strength index.", band: [70, 30], has_chart: true },
    { id: "sma50_vs_sma200", name: "50-day vs 200-day average", family: "trend", horizon: "long", min_history: 200, description: "Gap between averages.", band: [-10, 10], has_chart: true },
    { id: "pe_ratio", name: "Price to earnings", family: "value_quality", horizon: "long", min_history: 0, description: "Price over earnings.", band: null, has_chart: false },
  ],
};

export const STOCK: StockAlgos = {
  isin: ISIN, as_of: "2026-11-20", age_days: 1, stale: false, label: "research signal — not an order or investment advice",
  composite: { long: 62.5, short: 48, confidence: 0.75, agreement: 0.8, n_available: 3, n_total: 4 },
  rules: { long: 61, short: 55 }, with_algorithms: { long: 61, short: 55, weight: 0 },
  items: [
    { algo_id: "mom_3m", name: "3-month return", family: "momentum", horizon: "short", score: 70, value: 9.1, reason: "3-month return: 9.10%", as_of: "2026-11-20" },
    { algo_id: "rsi14", name: "RSI (14)", family: "momentum", horizon: "short", score: 38, value: 74, reason: "RSI (14): 74.00 (overbought)", as_of: "2026-11-20" },
    { algo_id: "sma50_vs_sma200", name: "50-day vs 200-day average", family: "trend", horizon: "long", score: 62.5, value: 2.5, reason: "x", as_of: "2026-11-20" },
    { algo_id: "pe_ratio", name: "Price to earnings", family: "value_quality", horizon: "long", score: null, value: null, reason: "needs trailing four-quarter EPS", as_of: "2026-11-20" },
  ],
};

export const SERIES: ChartSpec = {
  dates: ["2026-11-19", "2026-11-20"], caption: "Rolling return caption", as_of: "2026-11-20", markers: [],
  panels: [{ title: "Price", kind: "price", y_bands: [], series: [{ name: "Close", values: [1, 2], style: "line" }] }],
};

export const SECTOR: SectorAlgos = {
  macro_sector: "Information Technology", count: 2, label: STOCK.label,
  items: [
    { isin: ISIN, name: "Alpha Ltd", as_of: "2026-11-20", composite: STOCK.composite, family_scores: { momentum: 54, trend: 62.5, relative: 70 } },
    { isin: "INE002A01018", name: "Beta Ltd", as_of: "2026-11-20", composite: { long: 40, short: 66, confidence: 0.5, agreement: 0.6, n_available: 2, n_total: 4 }, family_scores: { momentum: 66, trend: 40 } },
  ],
};

export const UNIVERSE: AlgoUniverse = {
  algo: CATALOGUE.items[0], count: 2, label: STOCK.label,
  items: [
    { isin: ISIN, name: "Alpha Ltd", macro_sector: "Information Technology", score: 70, value: 9.1, reason: "3-month return: 9.10%", as_of: "2026-11-20" },
    { isin: "INE002A01018", name: "Beta Ltd", macro_sector: "Information Technology", score: 20, value: -12, reason: "3-month return: -12.00%", as_of: "2026-11-20" },
  ],
};

export const ROUTES = {
  "GET /algos": CATALOGUE,
  [`GET /stocks/${ISIN}/algos`]: STOCK,
  "GET /sectors/Information%20Technology/algos": SECTOR,
  "GET /sectors/Healthcare/algos": { ...SECTOR, macro_sector: "Healthcare", items: [], count: 0 },
  "GET /algos/mom_3m/universe": UNIVERSE,
};
