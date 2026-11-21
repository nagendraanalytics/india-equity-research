"use client";
import Link from "next/link";

import { familyLabel } from "../lib/algos";
import { useApi } from "../lib/useApi";
import type { SectorAlgos as SectorAlgosData } from "../lib/types";
import { Scatter, ScoreCell } from "./charts";
import { ScoreMatrix } from "./charts4";
import { Empty, ErrorBox, Loading, fmt } from "./ui";

export function SectorAlgos({ macro }: { macro: string }) {
  const res = useApi<SectorAlgosData>(`/sectors/${encodeURIComponent(macro)}/algos`);
  if (res.state === "loading") return <Loading what="sector algorithms" />;
  if (res.state === "error") return <ErrorBox message={res.message} />;
  const items = [...res.data.items].sort((a, b) => (b.composite?.long ?? -1) - (a.composite?.long ?? -1));
  const fams = Array.from(new Set(items.flatMap((i) => Object.keys(i.family_scores))));
  return (
    <section>
      <h1>Algorithms · {macro}</h1>
      <p><Link href="/algorithms">All algorithms</Link></p>
      <p className="label-banner">{res.data.label}</p>
      {items.length === 0 ? <Empty>No companies in this sector have algorithm scores yet.</Empty> : (
        <>
          <h2>Ranking by long-term composite</h2>
          <table>
            <thead><tr><th>Company</th><th>Long-term</th><th>Short-term</th><th>Agreement</th><th>Coverage</th><th>As of</th></tr></thead>
            <tbody>
              {items.map((i) => (
                <tr key={i.isin} data-testid="algo-sector-row">
                  <td><Link href={`/algorithms/stock/${encodeURIComponent(i.isin)}`}>{i.name}</Link></td>
                  <td><ScoreCell v={i.composite?.long ?? null} /></td>
                  <td><ScoreCell v={i.composite?.short ?? null} /></td>
                  <td>{i.composite?.agreement == null ? "—" : `${(100 * i.composite.agreement).toFixed(0)}%`}</td>
                  <td>{i.composite ? `${i.composite.n_available} of ${i.composite.n_total}` : "—"}</td>
                  <td className="muted">{i.as_of ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <h2>Long-term against short-term composite</h2>
          <Scatter dots={items.filter((i) => i.composite?.long != null && i.composite?.short != null)
            .map((i) => ({ label: i.name, x: i.composite!.long as number, y: i.composite!.short as number, rating: null, href: `/algorithms/stock/${i.isin}` }))} />
          <h2>Average score by company and family</h2>
          <ScoreMatrix title="Average algorithm score by company and family" rows={items.map((i) => i.name)} cols={fams.map(familyLabel)}
                       rowHref={(r) => `/algorithms/stock/${encodeURIComponent(items.find((i) => i.name === r)!.isin)}`}
                       cell={(r, c) => items.find((i) => i.name === r)!.family_scores[fams.find((f) => familyLabel(f) === c)!] ?? null} />
          <p className="note">Companies with a blank cell have no score for that family yet. Average of {fmt(items.length, 0)} companies shown.</p>
        </>
      )}
    </section>
  );
}
