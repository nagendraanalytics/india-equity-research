"use client";
import Link from "next/link";
import { useEffect, useState } from "react";

import { familyLabel, getSectorAlgos, pct } from "../lib/algos";
import { useApi } from "../lib/useApi";
import type { AlgoCatalogue, SectorAlgos } from "../lib/types";
import { Dial } from "./charts3";
import { HBars, Heatmap, Histogram } from "./charts2";
import { Empty, ErrorBox, Loading, fmt } from "./ui";

type Loaded = { state: "loading" } | { state: "error"; message: string } | { state: "ok"; sectors: SectorAlgos[] };

function Sectors({ cat }: { cat: AlgoCatalogue }) {
  const [res, setRes] = useState<Loaded>({ state: "loading" });
  const key = cat.macro_sectors.join("|");
  useEffect(() => {
    let live = true;
    Promise.all(cat.macro_sectors.map((m) => getSectorAlgos(m))).then(
      (sectors) => live && setRes({ state: "ok", sectors }),
      (e) => live && setRes({ state: "error", message: e instanceof Error ? e.message : String(e) }));
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  if (res.state === "loading") return <Loading what="sector scores" />;
  if (res.state === "error") return <ErrorBox message={res.message} />;
  const withData = res.sectors.filter((s) => s.items.length > 0);
  const all = withData.flatMap((s) => s.items.map((i) => ({ ...i, sector: s.macro_sector })));
  if (all.length === 0) return <Empty>No company has algorithm scores yet. They appear after each company's next run with price files.</Empty>;
  const fams = Array.from(new Set(all.flatMap((i) => Object.keys(i.family_scores))));
  const avg = (xs: number[]) => (xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null);
  const byLong = all.filter((i) => i.composite?.long != null).sort((a, b) => (b.composite!.long as number) - (a.composite!.long as number));
  const agree = avg(all.map((i) => i.composite?.agreement).filter((x): x is number => x != null));
  return (
    <>
      <h2>Average score by sector and family</h2>
      <Heatmap title="Average algorithm score by sector and family" rows={withData.map((s) => s.macro_sector)} cols={fams.map(familyLabel)}
               cell={(r, c) => {
                 const f = fams.find((x) => familyLabel(x) === c)!;
                 const v = avg(withData.find((s) => s.macro_sector === r)!.items.map((i) => i.family_scores[f]).filter((x): x is number => x != null));
                 return { value: Math.round(v ?? 0), color: "var(--accent)" };
               }} />
      <div className="strip">
        <div><h3>Long-term composite</h3><Histogram values={all.map((i) => i.composite?.long).filter((x): x is number => x != null)} title="Long-term composite across companies" /></div>
        <div><h3>Short-term composite</h3><Histogram values={all.map((i) => i.composite?.short).filter((x): x is number => x != null)} title="Short-term composite across companies" /></div>
        <Dial title="Average agreement" value={agree === null ? null : agree * 100} />
      </div>
      <h2>Most positive composite scores</h2>
      <ol>{byLong.slice(0, 5).map((i) => <li key={i.isin}><Link href={`/algorithms/stock/${encodeURIComponent(i.isin)}`}>{i.name}</Link> · {i.sector} · {fmt(i.composite?.long)}</li>)}</ol>
      <h2>Most negative composite scores</h2>
      <ol>{[...byLong].reverse().slice(0, 5).map((i) => <li key={i.isin}><Link href={`/algorithms/stock/${encodeURIComponent(i.isin)}`}>{i.name}</Link> · {i.sector} · {fmt(i.composite?.long)}</li>)}</ol>
      <h2>Coverage by sector</h2>
      <HBars title="Share of algorithms with a score, by sector"
             rows={withData.map((s) => ({ label: s.macro_sector, value: Math.round(100 * (avg(s.items.map((i) => i.composite?.confidence ?? 0)) ?? 0)) }))} />
      <p>{withData.map((s) => <span key={s.macro_sector}><Link href={`/algorithms/sector/${encodeURIComponent(s.macro_sector)}`}>{s.macro_sector}</Link>{" "}</span>)}</p>
    </>
  );
}

export function AlgosOverview() {
  const cat = useApi<AlgoCatalogue>("/algos");
  if (cat.state === "loading") return <Loading what="algorithms" />;
  if (cat.state === "error") return <ErrorBox message={cat.message} />;
  const c = cat.data;
  const fams = Array.from(new Set(c.items.map((a) => a.family)));
  return (
    <section>
      <h1>Algorithms</h1>
      <p>Algorithm weight in final scores: {pct(c.blend_weight)} ({c.blend_weight > 0 ? "on" : "off"}). Maximum allowed {pct(c.max_blend_weight)}.</p>
      <p className="muted">Every score is a 0–100 reading from price history. Placeholder bands, not calibrated; research output, not advice.</p>
      <Sectors cat={c} />
      <h2>All {c.count} algorithms</h2>
      {fams.map((f) => (
        <p key={f}><strong>{familyLabel(f)}:</strong>{" "}
          {c.items.filter((a) => a.family === f).map((a) => <span key={a.id}><Link href={`/algorithms/${encodeURIComponent(a.id)}`}>{a.name}</Link>{" · "}</span>)}
        </p>
      ))}
    </section>
  );
}
