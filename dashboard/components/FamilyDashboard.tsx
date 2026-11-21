"use client";
import Link from "next/link";
import { useEffect, useState } from "react";

import { FAMILIES, bandText, getUniverse } from "../lib/algos";
import { familyInsights } from "../lib/insights";
import { useApi } from "../lib/useApi";
import type { AlgoCatalogue, AlgoInfo, AlgoUniverse } from "../lib/types";
import { AlgoTabs } from "./AlgoTabs";
import { ScoreCell } from "./charts";
import { Histogram } from "./charts2";
import { Kpi, Strip, scoreColor } from "./charts4";
import { ErrorBox, Loading } from "./ui";

type Person = { isin: string; name: string; sector: string; scores: Record<string, number | null>; reasons: Record<string, string>; as_of: string };
type Uni = { state: "loading" } | { state: "error"; message: string } | { state: "ok"; data: AlgoUniverse[] };

function useUniverses(algos: AlgoInfo[]): Uni {
  const [res, setRes] = useState<Uni>({ state: "loading" });
  const key = algos.map((a) => a.id).join("|");
  useEffect(() => {
    let live = true;
    setRes({ state: "loading" });
    Promise.all(algos.map((a) => getUniverse(a.id))).then(
      (data) => live && setRes({ state: "ok", data }),
      (e) => live && setRes({ state: "error", message: e instanceof Error ? e.message : String(e) }));
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  return res;
}

export function FamilyDashboard({ family }: { family: string }) {
  const cat = useApi<AlgoCatalogue>("/algos");
  const info = FAMILIES.find((f) => f.id === family);
  const algos = cat.state === "ok" && info ? cat.data.items.filter((a) => a.family === family) : [];
  const uni = useUniverses(algos);
  const [sector, setSector] = useState("");
  if (cat.state === "loading") return <Loading what="analysis" />;
  if (cat.state === "error") return <ErrorBox message={cat.message} />;
  if (!info) return <ErrorBox message={`Unknown analysis family: ${family}`} />;
  const head = (<><AlgoTabs /><h1>{info.label} analysis</h1><p className="muted">{info.blurb}</p></>);
  if (uni.state === "loading") return <section>{head}<Loading what={`${info.label.toLowerCase()} scores`} /></section>;
  if (uni.state === "error") return <section>{head}<ErrorBox message={uni.message} /></section>;

  const universes = uni.data;
  const people = new Map<string, Person>();
  universes.forEach((u) => u.items.forEach((i) => {
    const row = people.get(i.isin) ?? { isin: i.isin, name: i.name, sector: i.macro_sector ?? "", scores: {}, reasons: {}, as_of: i.as_of };
    row.scores[u.algo.id] = i.score; row.reasons[u.algo.id] = i.reason;
    people.set(i.isin, row);
  }));
  const sectors = Array.from(new Set(Array.from(people.values()).map((p) => p.sector).filter(Boolean))).sort();
  const avgOf = (p: { scores: Record<string, number | null> }) => {
    const xs = Object.values(p.scores).filter((s): s is number => s !== null);
    return xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null;
  };
  const rows = Array.from(people.values()).filter((p) => (!sector || p.sector === sector))
    .map((p) => ({ ...p, avg: avgOf(p) as number | null })).filter((p): p is Person & { avg: number } => p.avg !== null)
    .sort((a, b) => b.avg - a.avg);
  if (rows.length === 0) {
    return (
      <section>{head}
        <p className="empty">No company has {info.label.toLowerCase()} scores yet. Scores appear after a company's next run with a price CSV in its folder
          (<code>prices__*.csv</code> under the local filings directory). Run <code>python -m ier.runtime nse-prices</code> to download them, re-run a company from the Agent console, then reload this page.</p>
      </section>
    );
  }
  const avgs = rows.map((r) => r.avg);
  const mean = avgs.reduce((a, b) => a + b, 0) / avgs.length;
  const posShare = Math.round((100 * avgs.filter((a) => a >= 55).length) / avgs.length);
  const best = rows[0], worst = rows[rows.length - 1];
  const insights = familyInsights(info.label.toLowerCase(),
    universes.map((u) => ({ name: u.algo.name, scores: u.items.filter((i) => rows.some((r) => r.isin === i.isin)).map((i) => i.score).filter((s): s is number => s !== null) })),
    rows.map((r) => ({ name: r.name, avg: r.avg })));
  const tone = mean >= 55 ? "ok" : mean <= 45 ? "bad" : "run";

  return (
    <section>
      {head}
      <div className="filters">
        <label>Sector <select value={sector} onChange={(e) => setSector(e.target.value)}>
          <option value="">All sectors</option>{sectors.map((s) => <option key={s} value={s}>{s}</option>)}
        </select></label>
      </div>
      <div className="tiles">
        <Kpi label="Companies scored" value={String(rows.length)} sub={`${algos.length} algorithms`} />
        <Kpi label="Average score" value={mean.toFixed(1)} tone={tone} sub={mean >= 55 ? "positive lean" : mean <= 45 ? "negative lean" : "neutral"} />
        <Kpi label="Lean positive" value={`${posShare}%`} sub="average 55 or more" tone={posShare >= 50 ? "ok" : undefined} />
        <Kpi label="Highest" value={best.avg.toFixed(0)} sub={best.name} tone="ok" />
        <Kpi label="Lowest" value={worst.avg.toFixed(0)} sub={worst.name} tone="bad" />
      </div>

      <div className="grid2">
        <div className="panel">
          <h2>Company by algorithm</h2>
          <div className="matrix-wrap">
            <table className="matrix" aria-label={`${info.label} scores by company and algorithm`}>
              <thead><tr><th>Company</th>{algos.map((a) => <th key={a.id} className="mh"><Link href={`/algorithms/${a.id}`} title={a.description}>{a.name}</Link></th>)}<th className="mh">Average</th></tr></thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.isin} data-testid="matrix-row">
                    <td><Link href={`/algorithms/stock/${encodeURIComponent(r.isin)}`}>{r.name}</Link></td>
                    {algos.map((a) => { const s = r.scores[a.id] ?? null; return (
                      <td key={a.id} className="mc" style={{ background: scoreColor(s) }} title={r.reasons[a.id] ?? ""}>{s === null ? "—" : s.toFixed(0)}</td>); })}
                    <td className="mc strong" style={{ background: scoreColor(r.avg) }}>{r.avg.toFixed(0)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="note">Red is a negative lean, green a positive lean, slate is neutral. Hover a cell for the reason.</p>
        </div>
        <div className="panel">
          <h2>What the scores say</h2>
          <ul className="insights">{insights.map((t) => <li key={t} data-testid="insight">{t}</li>)}</ul>
          <h3>Spread of {info.label.toLowerCase()} averages</h3>
          <Histogram values={avgs} title={`Distribution of ${info.label} averages`} />
        </div>
      </div>

      <h2>Each algorithm</h2>
      <div className="grid3">
        {algos.map((a) => {
          const u = universes.find((x) => x.algo.id === a.id)!;
          const items = u.items.filter((i) => i.score !== null && rows.some((r) => r.isin === i.isin)) as (typeof u.items[number] & { score: number })[];
          const sorted = [...items].sort((x, y) => y.score - x.score);
          return (
            <div key={a.id} className="panel">
              <h3><Link href={`/algorithms/${a.id}`}>{a.name}</Link></h3>
              <p className="note">{a.description} {bandText(a.band)}</p>
              <Strip name={a.name} dots={items.map((i) => ({ label: i.name, score: i.score }))} />
              {sorted.length > 0 ? (
                <table>
                  <tbody>
                    {[...sorted.slice(0, 3), ...(sorted.length > 6 ? sorted.slice(-3) : sorted.slice(3))].map((i) => (
                      <tr key={i.isin}><td><Link href={`/algorithms/stock/${encodeURIComponent(i.isin)}`}>{i.name}</Link></td><td><ScoreCell v={i.score} /></td><td className="muted">{i.reason}</td></tr>
                    ))}
                  </tbody>
                </table>
              ) : null}
            </div>
          );
        })}
      </div>
    </section>
  );
}
