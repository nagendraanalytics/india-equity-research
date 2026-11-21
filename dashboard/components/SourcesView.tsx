"use client";
import { useEffect, useState } from "react";

import { apiGet, apiPost } from "../lib/api";
import type { SourceItem, SourcesResponse } from "../lib/types";
import { ErrorBox, Loading } from "./ui";

export const STATUS_TEXT: Record<string, string> = {
  connected: "connected",
  needs_key: "needs key or login",
  waiting_for_file: "waiting for data or a file",
  stale: "connected, data is old",
  error: "error",
  not_checked: "not checked yet",
};

export function SourcesView() {
  const [data, setData] = useState<SourcesResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = () => apiGet<SourcesResponse>("/sources").then(setData, (e) => setError(e instanceof Error ? e.message : String(e)));
  useEffect(() => { void load(); }, []);

  async function test(id: string) {
    setBusy(id);
    setError(null);
    try {
      const one = await apiPost<SourceItem>(`/sources/${encodeURIComponent(id)}/check`);
      setData((d) => d && {
        ...d,
        segments: d.segments.map((s) => {
          const sources = s.sources.map((x) => (x.id === id ? one : x));
          return { ...s, sources, connected: sources.filter((x) => x.status === "connected").length };
        }),
      });
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  if (error && !data) return <ErrorBox message={error} />;
  if (!data) return <Loading what="sources" />;
  return (
    <section>
      <h1>Sources</h1>
      <p className="note" data-testid="sources-note">{data.note} {data.connected} of {data.total} sources connected.</p>
      {error ? <ErrorBox message={error} /> : null}
      {data.segments.map((seg) => (
        <div key={seg.id} data-testid="segment">
          <h2>{seg.name} <span className="badge">{seg.connected} of {seg.total} connected</span></h2>
          {seg.note ? <p className="note">{seg.note}</p> : null}
          <table>
            <thead><tr><th>Source</th><th>Status</th><th>Detail</th><th>Last checked</th><th>Terms</th><th></th></tr></thead>
            <tbody>
              {seg.sources.map((s) => (
                <tr key={s.id} data-testid="source-row">
                  <td>{s.name}<div className="muted">{s.refresh_minutes ? `every ${s.refresh_minutes} min while the watcher runs` : "on demand"}</div></td>
                  <td><span className={`badge ${s.status === "connected" ? "" : "badge-stale"}`} data-testid="source-status">{STATUS_TEXT[s.status] ?? s.status}</span></td>
                  <td>{s.detail ?? ""}</td>
                  <td>{s.last_checked_at ? new Date(s.last_checked_at).toLocaleString() : "never"}</td>
                  <td className="note">{s.terms ?? ""}</td>
                  <td><button disabled={busy === s.id} onClick={() => test(s.id)}>{busy === s.id ? "Testing…" : "Test"}</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}
    </section>
  );
}
