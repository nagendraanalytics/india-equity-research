"use client";
import Link from "next/link";

import { useApi } from "../lib/useApi";
import { Sparkline } from "./charts";
import type { HistoryResponse, StockDetail } from "../lib/types";
import { AlgoCard } from "./AlgoCard";
import { Freshness } from "./Freshness";
import { ErrorBox, Loading, RatingBadge, Value, fmt } from "./ui";

export function StockView({ isin }: { isin: string }) {
  const res = useApi<StockDetail>(`/stocks/${encodeURIComponent(isin)}`);
  const hist = useApi<HistoryResponse>(`/stocks/${encodeURIComponent(isin)}/history`);
  if (res.state === "loading") return <Loading what="stock" />;
  if (res.state === "error") return <ErrorBox message={res.message} />;
  const s = res.data;
  return (
    <section>
      <h1>{s.name} <small className="muted">{s.isin}</small></h1>
      <p className="muted">{[s.sector, s.industry, s.nse_code && `NSE ${s.nse_code}`, s.bse_code && `BSE ${s.bse_code}`].filter(Boolean).join(" · ")}</p>
      <p><Freshness as_of={s.as_of} age_days={s.age_days} stale={s.stale} /></p>
      <p>Rating: <RatingBadge rating={s.rating} /></p>
      <p className="label-banner">{s.label}</p>
      <p>Long-term score {fmt(s.long_term_score)} · short-term score {fmt(s.short_term_score)} · confidence {s.confidence ?? "not recorded"} · rules version {s.rules_version ?? "none"}</p>
      {s.signal ? (<><h2>Why this rating</h2><Value v={s.signal.reasons} /></>) : <p className="empty">No signal has been computed for this stock yet.</p>}
      <h2>Score history</h2>
      {hist.state === "ok" && hist.data.items.length > 0 ? (
        <p>
          Long-term <Sparkline label="Long-term score history" points={hist.data.items.map((p) => ({ date: p.run_date, value: p.long_term_score }))} />{" "}
          Short-term <Sparkline label="Short-term score history" points={hist.data.items.map((p) => ({ date: p.run_date, value: p.short_term_score }))} />
        </p>
      ) : <p className="muted">{hist.state === "loading" ? "Loading history…" : "No score history yet."}</p>}
      <AlgoCard isin={isin} />
      <h2>Sub-scores</h2><Value v={s.sub_scores} />
      <h2>Benefits</h2><Value v={s.benefits} />
      <h2>Risks</h2><Value v={s.risks} />
      <h2>Red flags</h2><div className="flags"><Value v={s.red_flags} /></div>
      {s.agent_id !== null ? <p><Link href={`/analyst/${s.agent_id}`}>Analyst view (findings, evidence, run history)</Link></p> : null}
      <p><Link href="/stocks">All stocks</Link></p>
    </section>
  );
}
