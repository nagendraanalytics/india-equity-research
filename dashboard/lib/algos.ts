import { apiGet } from "./api";
import type { AlgoCatalogue, AlgoUniverse, ChartSpec, SectorAlgos, StockAlgos } from "./types";

const e = encodeURIComponent;
export const getCatalogue = () => apiGet<AlgoCatalogue>("/algos");
export const getStockAlgos = (isin: string) => apiGet<StockAlgos>(`/stocks/${e(isin)}/algos`);
export const getSeries = (isin: string, algoId: string, points?: number) =>
  apiGet<ChartSpec>(`/stocks/${e(isin)}/algos/${e(algoId)}/series`, { points: points ? String(points) : undefined });
export const getSectorAlgos = (macro: string) => apiGet<SectorAlgos>(`/sectors/${e(macro)}/algos`);
export const getUniverse = (algoId: string) => apiGet<AlgoUniverse>(`/algos/${e(algoId)}/universe`);

/** Words for a 0–100 score; the number is always shown beside it, colour is never the only cue. */
export const lean = (score: number | null): string =>
  score === null ? "no score" : score >= 55 ? "positive lean" : score <= 45 ? "negative lean" : "neutral";
export const familyLabel = (f: string) => f.replace(/_/g, " ");
export const horizonLabel = (h: string) => (h === "long" ? "long-term" : "short-term");
export const bandText = (band: number[] | null) =>
  band ? `Scores 0 at ${band[0]} and 100 at ${band[1]} (placeholder band, not calibrated).` : "Needs data that is not available yet.";
export const pct = (w: number | null | undefined) => (w === null || w === undefined ? "not recorded" : `${(w * 100).toFixed(0)}%`);

export const FAMILIES: { id: string; label: string; blurb: string }[] = [
  { id: "trend", label: "Trend", blurb: "Is the price drifting up or down, and is that drift steady? Moving-average gaps, MACD and trend slope." },
  { id: "momentum", label: "Momentum", blurb: "How fast has the price moved recently? Returns over 1 to 12 months, rate of change and RSI." },
  { id: "risk", label: "Risk", blurb: "How rough has the ride been? Volatility, drawdown, downside deviation, risk-adjusted return and stretch versus average." },
  { id: "breakout", label: "Breakout", blurb: "Where does the price sit against its 52-week range and 20-day channel?" },
  { id: "volume", label: "Volume", blurb: "Is trading activity confirming the price move? On-balance volume and volume against its 20-day average." },
  { id: "relative", label: "Relative strength", blurb: "How does the stock do against its sector index and its sector peers?" },
];
