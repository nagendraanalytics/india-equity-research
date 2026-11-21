import { BANDS } from "./ratings";
import type { StockItem } from "./types";

/** Research stances, best first. These are labels for a reader, never instructions. */
export const STANCES = ["accumulate", "hold", "reduce", "avoid"] as const;
export type Stance = (typeof STANCES)[number];

export interface Signal {
  stance: Stance | null;
  /** true = the system has not rated this stock (low confidence); the stance is only a lean from the score. */
  provisional: boolean;
  basis: "long" | "short" | null;
  /** lower sorts first: rated stances 0-3, provisional leans 10-13, no score 99 */
  rank: number;
  text: string;
  score: number | null;
}

export function bandStance(score: number): Stance {
  if (score >= BANDS.accumulate) return "accumulate";
  if (score >= BANDS.hold) return "hold";
  if (score >= BANDS.reduce) return "reduce";
  return "avoid";
}

export function signalOf(s: StockItem): Signal {
  const rated = STANCES.find((x) => x === s.rating);
  const basis = s.long_term_score !== null ? "long" : s.short_term_score !== null ? "short" : null;
  const score = basis === "long" ? s.long_term_score : basis === "short" ? s.short_term_score : null;
  if (rated) return { stance: rated, provisional: false, basis, rank: STANCES.indexOf(rated), text: rated, score };
  if (score === null) return { stance: null, provisional: false, basis, rank: 99, text: "no score", score };
  const lean = bandStance(score);
  return { stance: lean, provisional: true, basis, rank: 10 + STANCES.indexOf(lean), text: `leans ${lean}`, score };
}

/** Best first: rated stance, then provisional leans, then unscored; higher score first inside each group. */
export function bySignal(a: StockItem, b: StockItem): number {
  const x = signalOf(a), y = signalOf(b);
  return x.rank - y.rank || (y.score ?? -1) - (x.score ?? -1) || a.name.localeCompare(b.name);
}
