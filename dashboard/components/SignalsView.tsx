"use client";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";

import { useApi } from "../lib/useApi";
import type { SignalsResponse } from "../lib/types";
import { Freshness, ThresholdNote } from "./Freshness";
import { Empty, ErrorBox, Loading } from "./ui";

export const RATINGS = ["accumulate", "hold", "reduce", "avoid", "insufficient evidence"];

export function SignalsView() {
  const [sector, setSector] = useState("");
  const [rating, setRating] = useState("");
  const seen = useRef<Set<string>>(new Set());
  const [sectors, setSectors] = useState<string[]>([]);
  const res = useApi<SignalsResponse>("/signals", { sector, rating });

  useEffect(() => {
    if (res.state !== "ok") return;
    const before = seen.current.size;
    for (const i of res.data.items) if (i.sector) seen.current.add(i.sector);
    if (seen.current.size !== before) setSectors([...seen.current].sort());
  }, [res]);

  return (
    <section>
      <h1>Signals</h1>
      <p data-testid="signals-label" className="label-banner">
        {res.state === "ok" ? res.data.label : "research signal — not an order or investment advice"}
      </p>
      <p className="note">Ratings are research labels produced by deterministic rules from sourced data.</p>
      <div className="filters">
        <label>Sector{" "}
          <select value={sector} onChange={(e) => setSector(e.target.value)}>
            <option value="">all sectors</option>
            {sectors.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
        </label>
        <label>Rating{" "}
          <select value={rating} onChange={(e) => setRating(e.target.value)}>
            <option value="">all ratings</option>
            {RATINGS.map((r) => <option key={r} value={r}>{r}</option>)}
          </select>
        </label>
      </div>
      {res.state === "loading" ? <Loading what="signals" /> : null}
      {res.state === "error" ? <ErrorBox message={res.message} /> : null}
      {res.state === "ok" ? (
        <>
          <ThresholdNote days={res.data.stale_threshold_days} status={res.data.stale_threshold_status} />
          {res.data.items.length === 0 ? <Empty>No signals match these filters (or none have been computed yet).</Empty> : (
            <table>
              <thead>
                <tr><th>Stock</th><th>Sector</th><th>Rating</th><th>Confidence</th><th>Freshness</th><th>Rules version</th><th>Reasons</th></tr>
              </thead>
              <tbody>
                {res.data.items.map((i) => (
                  <tr key={i.isin} data-testid="signal-row">
                    <td><Link href={`/stocks/${encodeURIComponent(i.isin)}`}>{i.name}</Link><div className="muted">{i.isin}</div></td>
                    <td>{i.sector ?? "—"}<div className="muted">{i.industry ?? ""}</div></td>
                    <td>
                      <span data-testid="rating" className={`badge rating rating-${i.rating.replace(/\s+/g, "-")}`}>{i.rating}</span>
                      <div className="label-row">{i.label}</div>
                    </td>
                    <td data-testid="confidence">{i.confidence ?? "not recorded"}</td>
                    <td><Freshness as_of={i.as_of} age_days={i.age_days} stale={i.stale} /></td>
                    <td>{i.rules_version}</td>
                    <td><ul>{i.reasons.map((r, k) => <li key={k}>{r}</li>)}</ul></td>
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
