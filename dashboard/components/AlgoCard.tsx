"use client";
import Link from "next/link";

import { lean, pct } from "../lib/algos";
import { useApi } from "../lib/useApi";
import type { StockAlgos } from "../lib/types";
import { fmt } from "./ui";

/** Company-page card: rules score beside the score with algorithms, and the strongest algorithm leans. */
export function AlgoCard({ isin }: { isin: string }) {
  const res = useApi<StockAlgos>(`/stocks/${encodeURIComponent(isin)}/algos`);
  if (res.state === "loading") return <p className="note">Loading algorithm scores…</p>;
  if (res.state === "error") return <p className="muted">Algorithm scores unavailable: {res.message}</p>;
  const d = res.data;
  const scored = d.items.filter((i) => i.score !== null) as Array<(typeof d.items)[number] & { score: number }>;
  const best = [...scored].sort((a, b) => b.score - a.score)[0];
  const worst = [...scored].sort((a, b) => a.score - b.score)[0];
  const on = (d.with_algorithms.weight ?? 0) > 0;
  return (
    <section className="algocard">
      <h2>Algorithms</h2>
      {d.items.length === 0 ? <p className="empty">No algorithm scores yet; they appear after the company's next run.</p> : (
        <>
          <p>Long-term: Rules score {fmt(d.rules.long)} / with algorithms {fmt(d.with_algorithms.long)}</p>
          <p>Short-term: Rules score {fmt(d.rules.short)} / with algorithms {fmt(d.with_algorithms.short)}</p>
          <p className="muted">Blend: weight {pct(d.with_algorithms.weight)}, {on ? "on" : "off"}.</p>
          {best && best.score >= 55 ? <p>Strongest {lean(best.score)}: {best.name} ({fmt(best.score)})</p> : null}
          {worst && worst.score <= 45 ? <p>Strongest {lean(worst.score)}: {worst.name} ({fmt(worst.score)})</p> : null}
        </>
      )}
      <p><Link href={`/algorithms/stock/${encodeURIComponent(isin)}`}>See all algorithms</Link></p>
    </section>
  );
}
