"use client";
import Link from "next/link";

import { useApi } from "../lib/useApi";
import type { SectorsResponse } from "../lib/types";
import { Freshness, ThresholdNote } from "./Freshness";
import { Empty, ErrorBox, Loading, fmt } from "./ui";

export function SectorsView() {
  const res = useApi<SectorsResponse>("/sectors");
  return (
    <section>
      <h1>Sectors</h1>
      {res.state === "ok" ? <p className="label-banner">{res.data.label}</p> : null}
      {res.state === "loading" ? <Loading what="sectors" /> : null}
      {res.state === "error" ? <ErrorBox message={res.message} /> : null}
      {res.state === "ok" ? (
        <>
          <ThresholdNote days={res.data.stale_threshold_days} status={res.data.stale_threshold_status} />
          {res.data.items.length === 0 ? <Empty>No sector data yet.</Empty> : (
            <table>
              <thead><tr><th>Sector</th><th>Industries</th><th>Stocks (scored)</th><th>Avg long-term</th><th>Avg short-term</th>
                <th>Weakest confidence</th><th>Signals</th><th>Rules version</th><th>Freshness (oldest scorecard)</th></tr></thead>
              <tbody>
                {res.data.items.map((s) => (
                  <tr key={s.sector} data-testid="sector-row">
                    <td><Link href={`/stocks?sector=${encodeURIComponent(s.sector)}`}>{s.sector}</Link></td>
                    <td>{s.industries.join(", ") || "—"}</td>
                    <td>{s.stock_count} ({s.scored_count})</td>
                    <td>{fmt(s.avg_long_term_score)}</td>
                    <td>{fmt(s.avg_short_term_score)}</td>
                    <td>{s.confidence ?? "—"}</td>
                    <td>{Object.entries(s.signal_counts).map(([k, v]) => `${k}: ${v}`).join(", ") || "—"}</td>
                    <td>{s.rules_versions.join(", ") || "—"}</td>
                    <td><Freshness as_of={s.as_of} age_days={s.age_days} stale={s.stale} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      ) : null}
    </section>
  );
}
