import type { AlgoScoreItem } from "./types";

const POS = 55, NEG = 45;
const median = (xs: number[]) => { const s = [...xs].sort((a, b) => a - b); const m = s.length >> 1; return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2; };

/** Plain-language readings of one stock's scores. Deterministic; never an instruction to trade. */
export function stockInsights(items: AlgoScoreItem[]): string[] {
  const scored = items.filter((i) => i.score !== null) as (AlgoScoreItem & { score: number })[];
  if (scored.length === 0) return ["No algorithm has a score for this stock yet."];
  const pos = scored.filter((i) => i.score >= POS), neg = scored.filter((i) => i.score <= NEG);
  const out = [`${pos.length} of ${scored.length} scored algorithms lean positive, ${neg.length} lean negative and ${scored.length - pos.length - neg.length} are neutral.`];
  const best = [...scored].sort((a, b) => b.score - a.score)[0], worst = [...scored].sort((a, b) => a.score - b.score)[0];
  if (best.score >= POS) out.push(`Strongest positive lean: ${best.name} (${best.score.toFixed(0)}). ${best.reason}`);
  if (worst.score <= NEG) out.push(`Strongest negative lean: ${worst.name} (${worst.score.toFixed(0)}). ${worst.reason}`);
  const fams = Array.from(new Set(scored.map((i) => i.family)));
  const avg = fams.map((f) => ({ f, v: scored.filter((i) => i.family === f).reduce((a, i) => a + i.score, 0) / scored.filter((i) => i.family === f).length }));
  const hi = avg.filter((a) => a.v >= POS).map((a) => a.f.replace(/_/g, " ")), lo = avg.filter((a) => a.v <= NEG).map((a) => a.f.replace(/_/g, " "));
  if (hi.length && lo.length) out.push(`Mixed picture: ${hi.join(", ")} lean positive while ${lo.join(", ")} lean negative.`);
  return out;
}

/** Readings across companies for one analysis family. */
export function familyInsights(family: string, algos: { name: string; scores: number[] }[], companies: { name: string; avg: number }[]): string[] {
  if (companies.length === 0) return [];
  const pos = companies.filter((c) => c.avg >= POS).length, neg = companies.filter((c) => c.avg <= NEG).length;
  const out = [`${pos} of ${companies.length} companies lean positive on ${family} (average score ${POS} or more); ${neg} lean negative.`];
  const usable = algos.filter((a) => a.scores.length > 0);
  if (usable.length) {
    const strong = [...usable].sort((a, b) => median(b.scores) - median(a.scores))[0];
    out.push(`${strong.name} has the highest typical score across companies (median ${median(strong.scores).toFixed(0)}).`);
    const wide = [...usable].sort((a, b) => (Math.max(...b.scores) - Math.min(...b.scores)) - (Math.max(...a.scores) - Math.min(...a.scores)))[0];
    const spread = Math.max(...wide.scores) - Math.min(...wide.scores);
    if (spread >= 30) out.push(`${wide.name} separates companies the most (scores span ${Math.min(...wide.scores).toFixed(0)} to ${Math.max(...wide.scores).toFixed(0)}).`);
  }
  const top = [...companies].sort((a, b) => b.avg - a.avg)[0], bottom = [...companies].sort((a, b) => a.avg - b.avg)[0];
  if (companies.length > 1) out.push(`Highest ${family} average: ${top.name} (${top.avg.toFixed(0)}). Lowest: ${bottom.name} (${bottom.avg.toFixed(0)}).`);
  return out;
}
