"use client";
import Link from "next/link";
import { useMemo, useState } from "react";

import { useApi } from "../lib/useApi";
import { RATINGS, ratingColor } from "../lib/ratings";
import { STANCES, bySignal, signalOf } from "../lib/signal";
import type { SectorsResponse, StockItem, StocksResponse } from "../lib/types";
import { RatingBar, ScoreBars, ScoreCell, Scatter } from "./charts";
import { Donut, Heatmap, Histogram } from "./charts2";
import { Freshness } from "./Freshness";
import { LiveFlow } from "./LiveFlow";
import { SignalChip } from "./SignalChip";
import { Empty, ErrorBox, Loading } from "./ui";

type SortKey = "name" | "sector" | "long" | "short" | "signal";

export function OverviewView() {
  const stocks = useApi<StocksResponse>("/stocks");
  const sectors = useApi<SectorsResponse>("/sectors");
  const [sector, setSector] = useState("");
  const [rating, setRating] = useState("");
  const [q, setQ] = useState("");
  const [sort, setSort] = useState<SortKey>("signal");
  const [stance, setStance] = useState("");

  const all = stocks.state === "ok" ? stocks.data.items : [];
  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    const keep = all.filter((s) =>
      (!sector || s.sector === sector) &&
      (!rating || (rating === "no rating" ? s.rating === null : s.rating === rating)) &&
      (!stance || signalOf(s).stance === stance) &&
      (!needle || `${s.name} ${s.nse_code ?? ""} ${s.bse_code ?? ""} ${s.isin}`.toLowerCase().includes(needle)));
    const num = (v: number | null) => (v === null ? -1 : v);
    const cmp: Record<SortKey, (a: StockItem, b: StockItem) => number> = {
      name: (a, b) => a.name.localeCompare(b.name),
      sector: (a, b) => (a.sector ?? "").localeCompare(b.sector ?? "") || a.name.localeCompare(b.name),
      long: (a, b) => num(b.long_term_score) - num(a.long_term_score) || a.name.localeCompare(b.name),
      short: (a, b) => num(b.short_term_score) - num(a.short_term_score) || a.name.localeCompare(b.name),
      signal: bySignal,
    };
    return [...keep].sort(cmp[sort]);
  }, [all, sector, rating, stance, q, sort]);

  if (stocks.state === "loading" || sectors.state === "loading") return <Loading what="overview" />;
  if (stocks.state === "error") return <ErrorBox message={stocks.message} />;
  if (sectors.state === "error") return <ErrorBox message={sectors.message} />;

  const counts: Record<string, number> = {};
  for (const s of rows) counts[s.rating ?? "no rating"] = (counts[s.rating ?? "no rating"] ?? 0) + 1;
  const scored = rows.filter((s) => s.long_term_score !== null);
  const lowConf = rows.filter((s) => s.rating === "insufficient evidence" || s.confidence === "low").length;
  const sectorRows = sectors.data.items
    .filter((s) => !sector || s.sector === sector)
    .map((s) => ({ label: s.sector, value: s.avg_long_term_score }));
  const top = scored.slice(0, 10).sort((a, b) => (b.long_term_score ?? 0) - (a.long_term_score ?? 0));
  const dots = rows.filter((s) => s.long_term_score !== null && s.short_term_score !== null)
    .map((s) => ({ label: s.name, x: s.long_term_score as number, y: s.short_term_score as number, rating: s.rating }));
  const confCount: Record<string, number> = {};
  for (const s of rows) confCount[s.confidence ?? "none"] = (confCount[s.confidence ?? "none"] ?? 0) + 1;
  const CONF_COLOR: Record<string, string> = { high: "#5fc38f", medium: "#38d4e8", low: "#f0b84e", none: "#46596a" };
  const confSlices = Object.entries(confCount).map(([label, value]) => ({ label, value, color: CONF_COLOR[label] ?? "#6d8296" }));
  const sectorNames = [...new Set(all.map((s) => s.sector).filter((x): x is string => !!x))].sort();
  const th = (key: SortKey, label: string) => (
    <th aria-sort={sort === key ? "descending" : undefined}><button type="button" onClick={() => setSort(key)}>{label}</button></th>
  );

  return (
    <section>
      <h1>Overview</h1>
      <p className="label-banner">{stocks.data.items[0]?.label ?? "research signal — not an order or investment advice"}</p>
      <p className="note">
        Ratings are research stances (accumulate / hold / reduce / avoid) from placeholder thresholds that have not been
        calibrated yet. A stock with low confidence is shown as insufficient evidence rather than a stance.
      </p>

      <LiveFlow />

      <div className="stance-strip" role="group" aria-label="Filter by stance">
        {STANCES.map((k) => {
          const mine = all.filter((s) => signalOf(s).stance === k);
          const lean = mine.filter((s) => signalOf(s).provisional).length;
          return (
            <button key={k} type="button" aria-pressed={stance === k} className={`stance stance-${k}${stance === k ? " on" : ""}`}
                    onClick={() => setStance(stance === k ? "" : k)}>
              <span className="stance-n">{mine.length}</span>
              <span className="stance-name">{k}</span>
              <span className="stance-sub">{lean ? `${mine.length - lean} rated · ${lean} provisional` : "rated"}</span>
            </button>
          );
        })}
      </div>

      <div className="tiles">
        <div className="tile"><b>{rows.length}</b><span>stocks shown</span></div>
        <div className="tile"><b>{scored.length}</b><span>with a score</span></div>
        <div className="tile"><b>{counts["accumulate"] ?? 0}</b><span>accumulate</span></div>
        <div className="tile"><b>{(counts["reduce"] ?? 0) + (counts["avoid"] ?? 0)}</b><span>reduce or avoid</span></div>
        <div className="tile"><b>{lowConf}</b><span>low confidence</span></div>
      </div>

      <div className="filters" role="search">
        <label>Search <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="name, code or ISIN" /></label>
        <label>Sector <select value={sector} onChange={(e) => setSector(e.target.value)}>
          <option value="">All sectors</option>{sectorNames.map((n) => <option key={n} value={n}>{n}</option>)}
        </select></label>
        <label>Rating <select value={rating} onChange={(e) => setRating(e.target.value)}>
          <option value="">All ratings</option>
          {[...RATINGS, "no rating"].map((n) => <option key={n} value={n}>{n}</option>)}
        </select></label>
      </div>

      {rows.length === 0 ? <Empty>No stocks match these filters.</Empty> : (
        <>
          <div className="grid2">
            <div className="panel"><h2>Rating distribution</h2><RatingBar counts={counts} /></div>
            <div className="panel"><h2>Sector average long-term score</h2>
              <ScoreBars title="Average long-term score by sector" rows={sectorRows} />
              <p className="note">Dashed lines mark the placeholder band edges at 30, 50 and 70.</p></div>
            <div className="panel"><h2>Highest long-term scores</h2>
              <ScoreBars title="Highest long-term scores" rows={top.map((s) => ({ label: s.name, value: s.long_term_score, color: ratingColor(s.rating) }))} /></div>
            <div className="panel"><h2>Long-term vs short-term</h2><Scatter dots={dots} /></div>
            <div className="panel"><h2>Score spread</h2>
              <Histogram title="Long-term score histogram" values={scored.map((s) => s.long_term_score as number)} />
              <p className="note">How many stocks fall in each 10-point band of the long-term score.</p></div>
            <div className="panel"><h2>Evidence confidence</h2>
              <Donut title="Stocks by confidence" slices={confSlices} /></div>
            <div className="panel"><h2>Sector by rating</h2>
              <Heatmap title="Stock count by sector and rating" rows={sectorNames.filter((n) => !sector || n === sector)} cols={[...RATINGS]}
                cell={(r, c) => ({ value: rows.filter((s) => s.sector === r && s.rating === c).length, color: ratingColor(c) })} /></div>
          </div>

          <h2>All stocks</h2>
          <table>
            <thead><tr>{th("name", "Stock")}{th("sector", "Sector")}{th("signal", "Signal")}{th("long", "Long-term")}{th("short", "Short-term")}<th>Confidence</th><th>Freshness</th></tr></thead>
            <tbody>
              {rows.map((s) => (
                <tr key={s.isin} data-testid="stock-row" className={`row-${signalOf(s).stance ?? "none"}`}>
                  <td><Link href={`/stocks/${encodeURIComponent(s.isin)}`}>{s.name}</Link> <small className="muted">{s.nse_code ?? s.bse_code ?? s.isin}</small></td>
                  <td>{s.sector ?? "—"}</td>
                  <td><SignalChip s={s} /></td>
                  <td><ScoreCell v={s.long_term_score} /></td>
                  <td><ScoreCell v={s.short_term_score} /></td>
                  <td>{s.confidence ?? "—"}</td>
                  <td><Freshness as_of={s.as_of} age_days={s.age_days} stale={s.stale} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </section>
  );
}
