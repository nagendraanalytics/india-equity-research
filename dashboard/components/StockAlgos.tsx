"use client";
import Link from "next/link";
import { useState } from "react";

import { bandText, familyLabel, pct } from "../lib/algos";
import { useApi } from "../lib/useApi";
import type { AlgoCatalogue, AlgoInfo, ChartSpec, StockAlgos as StockAlgosData } from "../lib/types";
import { AlgoBoard } from "./AlgoBoard";
import { Dial, Radar } from "./charts3";
import { Freshness } from "./Freshness";
import { PriceChart } from "./PriceChart";
import { ErrorBox, Loading } from "./ui";

function AlgoChart({ isin, info }: { isin: string; info: AlgoInfo }) {
  const res = useApi<ChartSpec>(`/stocks/${encodeURIComponent(isin)}/algos/${encodeURIComponent(info.id)}/series`, { points: "252" });
  return (
    <div>
      <p>{info.description}</p>
      <p className="note">{bandText(info.band)} Needs {info.min_history} daily prices.</p>
      {res.state === "loading" ? <Loading what="chart" /> : null}
      {res.state === "error" ? <ErrorBox message={res.message} /> : null}
      {res.state === "ok" ? <PriceChart spec={res.data} /> : null}
    </div>
  );
}

function ChartSection({ isin, info, score }: { isin: string; info: AlgoInfo; score: number | null }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="chart-section">
      <button type="button" aria-expanded={open} onClick={() => setOpen(!open)}>
        {open ? "Hide" : "Show"} chart: {info.name}
      </button>{" "}
      <span className="muted">{score === null ? "no score" : `score ${score.toFixed(1)}`}</span>
      {open ? <AlgoChart isin={isin} info={info} /> : null}
    </div>
  );
}

export function StockAlgos({ isin }: { isin: string }) {
  const res = useApi<StockAlgosData>(`/stocks/${encodeURIComponent(isin)}/algos`);
  const cat = useApi<AlgoCatalogue>("/algos");
  if (res.state === "loading") return <Loading what="algorithm scores" />;
  if (res.state === "error") return <ErrorBox message={res.message} />;
  const d = res.data;
  const byId = new Map((cat.state === "ok" ? cat.data.items : []).map((a) => [a.id, a]));
  const fams = Array.from(new Set(d.items.filter((i) => i.family !== "value_quality").map((i) => i.family)));
  const famAvg = fams.map((f) => {
    const xs = d.items.filter((i) => i.family === f && i.score !== null).map((i) => i.score as number);
    return xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null;
  });
  const c = d.composite;
  return (
    <section>
      <h1>Algorithms <small className="muted">{isin}</small></h1>
      <p><Link href={`/stocks/${encodeURIComponent(isin)}`}>Back to the stock</Link> · <Freshness as_of={d.as_of} age_days={d.age_days} stale={d.stale} /></p>
      <p className="label-banner">{d.label}</p>
      {d.items.length === 0 ? <p className="empty">No algorithm scores yet for this stock.</p> : (
        <>
          <h2>Summary</h2>
          <div className="strip">
            <Dial title="Long-term composite" value={c?.long ?? null} />
            <Dial title="Short-term composite" value={c?.short ?? null} />
            <div>
              {c ? <p>{(100 * (c.agreement ?? 0)).toFixed(0)}% of algorithms agree on the direction.</p> : null}
              {c ? <p>{c.n_available} of {c.n_total} algorithms have a score.</p> : null}
              <p className="muted">Rules score {d.rules.long?.toFixed(1) ?? "—"} long / {d.rules.short?.toFixed(1) ?? "—"} short; with algorithms {d.with_algorithms.long?.toFixed(1) ?? "—"} / {d.with_algorithms.short?.toFixed(1) ?? "—"} (weight {pct(d.with_algorithms.weight)}).</p>
            </div>
            <Radar title="Average score by family" labels={fams.map(familyLabel)} values={famAvg} />
          </div>
          <h2>Scoreboard</h2>
          <AlgoBoard items={d.items} />
          <h2>Charts</h2>
          {d.items.map((i) => {
            const info = byId.get(i.algo_id);
            return info?.has_chart ? <ChartSection key={i.algo_id} isin={isin} info={info} score={i.score} /> : null;
          })}
        </>
      )}
    </section>
  );
}
