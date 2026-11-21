"use client";
import { useApi } from "../lib/useApi";
import { safeHttpUrl } from "../lib/safe";
import type { Analyst } from "../lib/types";
import { Freshness } from "./Freshness";
import { ErrorBox, Loading, Value, fmt } from "./ui";

export function AnalystView({ agentId }: { agentId: number }) {
  const res = useApi<Analyst>(`/analysts/${agentId}`);
  if (res.state === "loading") return <Loading what="analyst" />;
  if (res.state === "error") return <ErrorBox message={res.message} />;
  const a = res.data;
  const f = a.findings;
  return (
    <section>
      <h1>{a.name} <small className="muted">analyst view · {a.role}{a.isin ? ` · ${a.isin}` : ""}</small></h1>
      <p><Freshness {...a.freshness} /></p>
      <p>Confidence: <b data-testid="confidence">{a.confidence ?? "not recorded"}</b> · Rules version: {a.rules_version ?? "none"}</p>

      <h2>Findings</h2>
      {f === null ? <p className="empty">No scorecard yet, so there are no findings for this analyst.</p> : (
        <>
          <p>Long-term score {fmt(f.long_term_score)} · short-term score {fmt(f.short_term_score)}</p>
          <h3>Sub-scores</h3><Value v={f.sub_scores} />
          <h3>Benefits</h3><Value v={f.benefits} />
          <h3>Risks</h3><Value v={f.risks} />
          <h3>Red flags</h3><div className="flags"><Value v={f.red_flags} /></div>
        </>
      )}

      <h2>Cited evidence</h2>
      {a.evidence.length === 0 ? <p className="empty">No cited evidence recorded.</p> : (
        <ul className="evidence">
          {a.evidence.map((e, i) => {
            const href = e.source ? safeHttpUrl(e.source.url) : null;
            return (
              <li key={i} data-testid="evidence-item">
                <strong>{e.claim_type}{e.claim_ref ? `: ${e.claim_ref}` : ""}</strong>
                {e.fact ? (
                  <div>Value: {e.fact.value} {e.fact.unit ?? ""} ({e.fact.period}{e.fact.as_of ? `, as of ${e.fact.as_of}` : ""})
                    {e.fact.conflict ? <span className="badge badge-stale">conflicting sources</span> : null}</div>
                ) : null}
                {e.source ? (
                  <div>
                    {href
                      ? <a href={href} target="_blank" rel="noopener noreferrer">{e.source.url}</a>
                      : <><span>{e.source.url}</span> <span className="muted">(not an http(s) link, shown as text)</span></>}
                    <div className="muted">Type: {e.source.type} · Tier {e.source.tier}
                      {e.source.publish_date ? ` · Published ${e.source.publish_date}` : " · Publish date unknown"}
                      {e.source.fetched_at ? ` · Fetched ${e.source.fetched_at}` : ""}</div>
                  </div>
                ) : <div className="muted">No source recorded for this claim.</div>}
              </li>
            );
          })}
        </ul>
      )}

      <h2>Run history</h2>
      {a.run_history.length === 0 ? <p className="empty">No runs logged.</p> : (
        <table>
          <thead><tr><th>When</th><th>Run</th><th>Role</th><th>Model</th><th>Status</th><th>Tokens</th><th>Model seconds</th></tr></thead>
          <tbody>
            {a.run_history.map((r) => (
              <tr key={r.run_log_id}>
                <td>{r.created_at ?? "—"}</td><td>{r.run_id ?? "—"}</td><td>{r.role ?? "—"}</td>
                <td>{r.model_version ?? "—"}</td><td>{r.status ?? "—"}</td><td>{r.total_tokens}</td><td>{r.model_seconds.toFixed(1)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h2>Rule inputs</h2>
      <pre data-testid="rule-inputs">{JSON.stringify(a.rule_inputs, null, 2)}</pre>
    </section>
  );
}
