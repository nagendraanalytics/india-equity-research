"use client";
import Link from "next/link";
import { useState } from "react";

import { FAMILIES, bandText, familyLabel, pct } from "../lib/algos";
import { stockInsights } from "../lib/insights";
import { useApi } from "../lib/useApi";
import type { AlgoCatalogue, AlgoInfo, AlgoScoreItem, ChartSpec, StockAlgos as StockAlgosData } from "../lib/types";
import { AlgoBoard } from "./AlgoBoard";
import { Dial, Radar } from "./charts3";
import { Kpi, scoreColor } from "./charts4";
import { AlgoTabs } from "./AlgoTabs";
import { Freshness } from "./Freshness";
import { PriceChart } from "./PriceChart";
import { ErrorBox, Loading } from "./ui";

function AlgoChart({ isin, info, reading }: { isin: string; info: AlgoInfo; reading: string }) {
  const res = useApi<ChartSpec>(`/stocks/${encodeURIComponent(isin)}/algos/${encodeURIComponent(info.id)}/series`, { points: "252" });
  return (
    <div role="tabpanel">
      <p><b>{reading}</b></p>
      <p className="note">{info.description} {bandText(info.band)} Needs {info.min_history} daily prices.</p>
      {res.state === "loading" ? <Loading what="chart" /> : null}
      {res.state === "error" ? <ErrorBox message={res.message} /> : null}
      {res.state === "ok" ? <PriceChart spec={res.data} /> : null}
    </div>
  );
}

function FamilyPanel({ isin, family, items, byId }: { isin: string; family: string; items: AlgoScoreItem[]; byId: Map<string, AlgoInfo> }) {
  const charted = items.filter((i) => byId.get(i.algo_id)?.has_chart);
  const [sel, setSel] = useState(charted[0]?.algo_id ?? "");
  const scored = items.filter((i) => i.score !== null) as (AlgoScoreItem & { score: number })[];
  const avg = scored.length ? scored.reduce((a, i) => a + i.score, 0) / scored.length : null;
  const info = byId.get(sel);
  const cur = items.find((i) => i.algo_id === sel);
  const meta = FAMILIES.find((f) => f.id === family);
  return (
    <div className="panel family-panel">
      <div className="fp-head">
        <div>
          <h2>{familyLabel(family)}{family === "value_quality" ? " (phase 2)" : ""}</h2>
          {meta ? <p className="note">{meta.blurb}</p> : null}
        </div>
        <div className="fp-avg" style={{ borderColor: scoreColor(avg) }}>
          <b>{avg === null ? "—" : avg.toFixed(0)}</b><span className="note">family average</span>
        </div>
      </div>
      {charted.length === 0 ? (
        <p className="note">Needs earnings data, planned for phase 2: {items.map((i) => i.name).join(", ")}.</p>
      ) : (
        <>
          <div role="tablist" aria-label={`${familyLabel(family)} algorithms`} className="algo-tabs">
            {charted.map((i) => (
              <button key={i.algo_id} type="button" role="tab" aria-selected={i.algo_id === sel} className="algo-tab" onClick={() => setSel(i.algo_id)}
                      style={{ borderTopColor: scoreColor(i.score) }}>
                <span>{i.name}</span><b>{i.score === null ? "—" : i.score.toFixed(0)}</b>
              </button>
            ))}
          </div>
          {info && cur ? <AlgoChart key={sel} isin={isin} info={info} reading={cur.reason} /> : null}
        </>
      )}
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
  const allFams = Array.from(new Set(d.items.map((i) => i.family)));
  const fams = allFams.filter((f) => f !== "value_quality");
  const famAvg = fams.map((f) => {
    const xs = d.items.filter((i) => i.family === f && i.score !== null).map((i) => i.score as number);
    return xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null;
  });
  const c = d.composite;
  const insights = stockInsights(d.items);
  return (
    <section>
      <AlgoTabs />
      <h1>Algorithms <small className="muted">{isin}</small></h1>
      <p><Link href={`/stocks/${encodeURIComponent(isin)}`}>Back to the stock</Link> · <Freshness as_of={d.as_of} age_days={d.age_days} stale={d.stale} /></p>
      <p className="label-banner">{d.label}</p>
      {d.items.length === 0 ? <p className="empty">No algorithm scores yet for this stock. They appear after the company's next run with a price CSV in its folder.</p> : (
        <>
          <div className="tiles">
            <Kpi label="Rules score (long / short)" value={`${d.rules.long?.toFixed(0) ?? "—"} / ${d.rules.short?.toFixed(0) ?? "—"}`} sub="from the rule set" />
            <Kpi label="With algorithms" value={`${d.with_algorithms.long?.toFixed(0) ?? "—"} / ${d.with_algorithms.short?.toFixed(0) ?? "—"}`} sub={`algorithm weight ${pct(d.with_algorithms.weight)}`} />
            <Kpi label="Agreement" value={c ? `${(100 * (c.agreement ?? 0)).toFixed(0)}%` : "—"} sub="of algorithms agree on the direction" />
            <Kpi label="Coverage" value={c ? `${c.n_available}/${c.n_total}` : "—"} sub="algorithms with a score" />
          </div>
          <div className="grid2">
            <div className="panel">
              <h2>What the scores say</h2>
              <ul className="insights">{insights.map((t) => <li key={t} data-testid="insight">{t}</li>)}</ul>
              <div className="strip">
                <Dial title="Long-term composite" value={c?.long ?? null} />
                <Dial title="Short-term composite" value={c?.short ?? null} />
              </div>
              <p>{c ? `${(100 * (c.agreement ?? 0)).toFixed(0)}% of algorithms agree on the direction. ${c.n_available} of ${c.n_total} algorithms have a score.` : ""}</p>
              <p className="muted">Rules score {d.rules.long?.toFixed(1) ?? "—"} long / {d.rules.short?.toFixed(1) ?? "—"} short; with algorithms {d.with_algorithms.long?.toFixed(1) ?? "—"} / {d.with_algorithms.short?.toFixed(1) ?? "—"} (weight {pct(d.with_algorithms.weight)}).</p>
            </div>
            <div className="panel radar-panel">
              <h2>Shape across analyses</h2>
              <div style={{ maxWidth: 360, margin: "0 auto" }}><Radar title="Average score by family" labels={fams.map(familyLabel)} values={famAvg} /></div>
              <p className="note">Each spoke is the average of that family's algorithm scores. Further out is a more positive lean.</p>
            </div>
          </div>
          <h2>Analyses</h2>
          <div className="grid-wide">
            {allFams.map((f) => <FamilyPanel key={f} isin={isin} family={f} items={d.items.filter((i) => i.family === f)} byId={byId} />)}
          </div>
          <h2>All readings</h2>
          <AlgoBoard items={d.items} />
        </>
      )}
    </section>
  );
}
