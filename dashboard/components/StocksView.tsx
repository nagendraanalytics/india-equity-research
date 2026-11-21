"use client";
import Link from "next/link";
import { useState } from "react";

import { useApi } from "../lib/useApi";
import type { StocksResponse } from "../lib/types";
import { Freshness, ThresholdNote } from "./Freshness";
import { Empty, ErrorBox, Loading, RatingBadge, fmt } from "./ui";

const COLUMNS: Array<{ key: string; label: string }> = [
  { key: "name", label: "Stock" }, { key: "sector", label: "Sector" }, { key: "industry", label: "Industry" },
  { key: "long_term_score", label: "Long-term score" }, { key: "short_term_score", label: "Short-term score" },
  { key: "as_of", label: "Freshness" },
];

export function StocksView({ sector, industry }: { sector?: string; industry?: string }) {
  const [sort, setSort] = useState("name");
  const res = useApi<StocksResponse>("/stocks", { sector, industry, sort });
  const toggle = (key: string) => setSort(sort === key ? `-${key}` : key);
  const arrow = (key: string) => (sort === key ? " ▲" : sort === `-${key}` ? " ▼" : "");
  return (
    <section>
      <h1>Stocks{sector ? ` · ${sector}` : ""}{industry ? ` · ${industry}` : ""}</h1>
      <p><Link href="/sectors">Back to sectors</Link></p>
      {res.state === "loading" ? <Loading what="stocks" /> : null}
      {res.state === "error" ? <ErrorBox message={res.message} /> : null}
      {res.state === "ok" ? (
        <>
          <ThresholdNote days={res.data.stale_threshold_days} status={res.data.stale_threshold_status} />
          {res.data.items.length === 0 ? <Empty>No stocks match.</Empty> : (
            <table>
              <thead>
                <tr>
                  {COLUMNS.map((c) => (
                    <th key={c.key}><button type="button" className="link" aria-label={`Sort by ${c.label}`} onClick={() => toggle(c.key)}>{c.label}{arrow(c.key)}</button></th>
                  ))}
                  <th>Confidence</th><th>Rating</th>
                </tr>
              </thead>
              <tbody>
                {res.data.items.map((s) => (
                  <tr key={s.isin} data-testid="stock-row">
                    <td><Link href={`/stocks/${encodeURIComponent(s.isin)}`}>{s.name}</Link><div className="muted">{s.isin}</div></td>
                    <td>{s.sector ?? "—"}</td><td>{s.industry ?? "—"}</td>
                    <td>{fmt(s.long_term_score)}</td><td>{fmt(s.short_term_score)}</td>
                    <td><Freshness as_of={s.as_of} age_days={s.age_days} stale={s.stale} /></td>
                    <td>{s.confidence ?? "—"}</td>
                    <td><RatingBadge rating={s.rating} />{s.rating ? <div className="label-row">{s.label}</div> : null}</td>
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
