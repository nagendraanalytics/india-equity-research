"use client";
import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError, apiGet, apiPost } from "../lib/api";
import type { AgentDetail, AgentEvent, HistoryResponse, RerunResponse, StockDetail, TreeNode, TreeResponse } from "../lib/types";
import { Sparkline, ScoreCell } from "./charts";
import { STATUS_COLORS } from "./FleetView";
import { buildRuns } from "./PipelineView";
import { ErrorBox, Loading, RatingBadge } from "./ui";

export const COMPANY_POLL_MS = 4_000;

/** The agents one company is meant to have. `built: false` ones are the roadmap, shown so it is
 *  clear what exists today and what would wake up on a change once it does. */
export const ROSTER = [
  { key: "fundamentals", title: "Financials", built: true,
    does: "Reads the company's filings and pulls reported figures: margins, attrition, debt, pledging.",
    wakes: "A new or changed filing file for this company" },
  { key: "technicals", title: "Price and technicals", built: true,
    does: "Computes trend, momentum and strength from a daily price file. Feeds the short-term score.",
    wakes: "A new or changed prices__*.csv file" },
  { key: "news", title: "News and announcements", built: true,
    does: "Reads exchange announcements and news and lists the events, with the exact passage as proof. Shown here only; it does not change the score.",
    wakes: "A new or changed news__*.txt/.pdf/.html file" },
  { key: "social", title: "Social and data feeds", built: true,
    does: "Reads exported social posts (for example from X) and lists events. Unverified and shown here only; it does not change the score.",
    wakes: "A new or changed social__*.csv/.txt/.json file" },
  { key: "synthesis", title: "Scoring and verification", built: true,
    does: "Combines the facts into the long-term and short-term scores and the rating, and checks that every claim traces back to a cited source.",
    wakes: "Any agent above finishing with new facts" },
] as const;

interface FeedItem { headline: string; quote: string; file: string; event_type: string; sentiment: string; severity: string; verified: boolean }
interface FeedPayload { kind?: string; files?: string[]; items?: FeedItem[]; counts?: Record<string, number> }
const SENT_COLOR: Record<string, string> = { positive: "status-completed", negative: "status-failed", neutral: "status-idle" };
const DROP_HINT: Record<string, string> = {
  technicals: "No price file yet. Save a daily price CSV (date, close, volume) as prices__daily__YYYY-MM-DD.csv in this company's folder.",
  news: "No news files yet. Save announcements or articles as news__<name>__YYYY-MM-DD.txt (or .pdf/.html) in this company's folder.",
  social: "No social files yet. Save exported posts as social__<name>__YYYY-MM-DD.csv (or .txt/.json) in this company's folder.",
};


const FLOW = ["Change detected", "Agents gather data", "Scored and verified", "Score updated"];

function findNode(n: TreeNode, id: number): TreeNode | null {
  if (n.id === id) return n;
  for (const c of n.children) { const r = findNode(c, id); if (r) return r; }
  return null;
}
const dur = (ms: number) => (ms < 1000 ? "<1 s" : ms < 60_000 ? `${Math.round(ms / 1000)} s` : `${(ms / 60_000).toFixed(1)} min`);

/** Which step of the change→score flow is active, from the workers' statuses. */
export function flowStep(workers: TreeNode[], companyStatus: string): number {
  const run = (k: string) => workers.some((w) => w.name.toLowerCase().includes(k) && w.status === "running");
  if (run("synthesis")) return 2;
  if (workers.some((w) => w.status === "running") || companyStatus === "running") return 1;
  if (companyStatus === "queued" || companyStatus === "blocked") return 0;
  return 3;
}

function Technicals({ out, hint }: { out: unknown; hint: string }) {
  const o = out as { as_of?: string | null; metrics?: Record<string, number>; files?: number } | null | undefined;
  if (!o || !o.files) return <p className="note">{hint}</p>;
  const m = o.metrics ?? {};
  const rows: [string, string][] = [
    ["Price vs 200-day average", m.price_vs_sma200 !== undefined ? `${m.price_vs_sma200.toFixed(1)}%` : "not enough history"],
    ["3-month momentum", m.momentum_3m !== undefined ? `${m.momentum_3m.toFixed(1)}%` : "not enough history"],
    ["Strength index (RSI 14)", m.rsi14 !== undefined ? m.rsi14.toFixed(0) : "not enough history"]];
  return (
    <dl className="kv tech" aria-label="Technicals">
      {rows.map(([k, v]) => <div key={k}><dt>{k}</dt><dd>{v}</dd></div>)}
      <div><dt>Prices to</dt><dd>{o.as_of ?? "unknown"}</dd></div>
    </dl>
  );
}

function Feed({ out, hint, social }: { out: FeedPayload | null | undefined; hint: string; social: boolean }) {
  if (!out || !out.files || out.files.length === 0) return <p className="note">{hint}</p>;
  const items = out.items ?? [];
  return (
    <div>
      <p className="note">{out.files.length} file{out.files.length === 1 ? "" : "s"} read · {items.length} event{items.length === 1 ? "" : "s"}{social ? " · unverified" : ""}</p>
      <ul className="feed-items">
        {items.slice(0, 5).map((i, k) => (
          <li key={k}><span className={`badge status ${SENT_COLOR[i.sentiment] ?? "status-idle"}`}>{i.sentiment}</span> {i.headline}
            <span className="note"> · {i.event_type} · {i.severity} · {i.file}</span></li>
        ))}
      </ul>
      {items.length === 0 ? <p className="note">No events found in these files.</p> : null}
    </div>
  );
}

export function CompanyView({ agentId, pollMs = COMPANY_POLL_MS }: { agentId: number; pollMs?: number }) {
  const [node, setNode] = useState<TreeNode | null>(null);
  const [detail, setDetail] = useState<AgentDetail | null>(null);
  const [events, setEvents] = useState<AgentEvent[]>([]);
  const [outputs, setOutputs] = useState<Record<string, unknown>>({});
  const [stock, setStock] = useState<StockDetail | null>(null);
  const [hist, setHist] = useState<HistoryResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const inflight = useRef(false);

  useEffect(() => {
    let dead = false;
    const load = async () => {
      try {
        const [t, a, d] = await Promise.all([apiGet<TreeResponse>("/agents/tree"),
          apiGet<{ events: AgentEvent[] }>("/agents/activity", { limit: "800" }), apiGet<AgentDetail>(`/agents/${agentId}`)]);
        if (dead) return;
        const n = findNode(t.tree, agentId);
        if (!n) { setError(`agent ${agentId} is not in the tree`); return; }
        setNode(n); setEvents(a.events); setDetail(d); setError(null);
        const feedIds = n.children.filter((w) => ["technicals", "news", "social"].includes(w.name.toLowerCase()));
        const got = await Promise.allSettled(feedIds.map((w) => apiGet<AgentDetail>(`/agents/${w.id}`)));
        if (!dead) setOutputs(Object.fromEntries(feedIds.map((w, i) => [w.name.toLowerCase(),
          got[i].status === "fulfilled" ? (got[i] as PromiseFulfilledResult<AgentDetail>).value.last_output?.payload ?? null : null])));
        if (d.isin) {
          const [s, h] = await Promise.allSettled([apiGet<StockDetail>(`/stocks/${encodeURIComponent(d.isin)}`),
            apiGet<HistoryResponse>(`/stocks/${encodeURIComponent(d.isin)}/history`)]);
          if (!dead) { setStock(s.status === "fulfilled" ? s.value : null); setHist(h.status === "fulfilled" ? h.value : null); }
        }
      } catch (e) { if (!dead) setError(e instanceof Error ? e.message : String(e)); }
    };
    void load();
    const timer = setInterval(load, pollMs);
    return () => { dead = true; clearInterval(timer); };
  }, [agentId, pollMs]);

  const rerun = useCallback(async () => {
    if (inflight.current) return;
    inflight.current = true; setBusy(true); setMsg(null);
    try {
      const r = await apiPost<RerunResponse>(`/agents/${agentId}/rerun`);
      setMsg({ ok: true, text: r.already_queued ? `Re-run already queued (job ${r.job_id})` : `Re-run queued (job ${r.job_id})` });
    } catch (e) {
      const st = e instanceof ApiError ? e.status : 0;
      setMsg({ ok: false, text: `Re-run failed${st ? ` (${st})` : ""}: ${e instanceof Error ? e.message : String(e)}` });
    } finally { inflight.current = false; setBusy(false); }
  }, [agentId]);

  if (error && !node) return <ErrorBox message={error} />;
  if (!node || !detail) return <Loading what="company agents" />;

  const workers = node.children;
  const mine = new Set([node.id, ...workers.map((w) => w.id)]);
  const feed = events.filter((e) => mine.has(e.agent_id)).slice(0, 15);
  const runs = buildRuns(events);
  const name = (id: number) => (id === node.id ? node.name : workers.find((w) => w.id === id)?.name ?? `agent ${id}`);
  const cards = ROSTER.map((r) => {
    const w = r.built ? workers.find((x) => x.name.toLowerCase().includes(r.key)) ?? null : null;
    const last = w ? runs.filter((x) => x.agentId === w.id).sort((a, b) => b.start - a.start)[0] : undefined;
    return { r, w, last };
  });
  const active = workers.filter((w) => w.status === "running").length;
  const step = flowStep(workers, node.status);
  const pts = (hist?.items ?? []);

  return (
    <section>
      <p className="note"><Link href="/pipeline">← Pipeline</Link></p>
      <h1>{node.name} <span className={`badge status status-${node.status}`}>{node.status}</span></h1>
      {detail.last_error ? (node.status === "failed" || node.status === "degraded"
        ? <p className="error" role="alert">Last failure: {detail.last_error.message ?? "no message"}</p>
        : <p className="note">Earlier failure, since recovered: {detail.last_error.message ?? "no message"}</p>) : null}

      <div className="tiles">
        <div className="tile tile-run"><b data-testid="co-active">{active}</b><span>agents working now</span></div>
        <div className="tile"><b>{ROSTER.length}</b><span>agents for this company</span></div>
        <div className="tile"><b>{detail.cost.cost_today.toFixed(2)}</b><span>cost today</span></div>
        <div className="tile"><b>{stock?.long_term_score != null ? stock.long_term_score.toFixed(1) : "—"}</b><span>long-term score</span></div>
        <div className="tile"><b>{stock?.short_term_score != null ? stock.short_term_score.toFixed(1) : "—"}</b><span>short-term score</span></div>
      </div>

      <div className="panel">
        <h2>When something changes</h2>
        <ol className="stages" aria-label="Change to score flow">
          {FLOW.map((f, i) => (
            <li key={f} className={`stage${i === step && step < 3 ? " on" : ""}`}>
              <div className="flow-step" data-state={i < step || step === 3 ? "done" : i === step ? "active" : "pending"}>
                <span className="stage-name">{f}</span>
                <span className="stage-note">{i < step || step === 3 ? "done" : i === step ? "in progress" : "waiting"}</span>
              </div>
            </li>
          ))}
        </ol>
        <p className="note">A new filing, announcement or post wakes the matching agent. When it finds something new, scoring and verification run again and the score is updated. If nothing changed, the run is skipped.</p>
      </div>

      <h2>Agents for this company</h2>
      <ul className="cards roster">
        {cards.map(({ r, w, last }) => (
          <li key={r.key} className={`card${r.built ? "" : " planned"}`} data-testid={`roster-${r.key}`}>
            <div className="roster-head"><b>{r.title}</b>
              {r.built ? <span className={`badge status status-${w?.status ?? "idle"}`}>{w?.status ?? "not started"}</span>
                : <span className="badge">not built yet</span>}</div>
            <p className="muted">{r.does}</p>
            <p className="note">Wakes on: {r.wakes}</p>
            {w?.current_task ? <p>Doing: {w.current_task}</p> : null}
            {r.key === "technicals" ? <Technicals out={outputs.technicals} hint={DROP_HINT.technicals} /> : null}
            {r.key === "news" || r.key === "social" ? <Feed out={outputs[r.key] as FeedPayload | null | undefined} hint={DROP_HINT[r.key]} social={r.key === "social"} /> : null}
            {last ? <p className="note">Last run {last.outcome}, took {dur((last.end ?? Date.now()) - last.start)} · cost {w!.cost_today.toFixed(2)}</p> : null}
          </li>
        ))}
      </ul>

      <div className="grid2">
        <div className="panel">
          <h2>Score</h2>
          {stock ? (
            <>
              <p><RatingBadge rating={stock.rating} /> <span className="muted">confidence {stock.confidence ?? "not recorded"}</span></p>
              <p>Long-term <ScoreCell v={stock.long_term_score} /> · Short-term <ScoreCell v={stock.short_term_score} /></p>
              {pts.length > 1 ? <Sparkline label="Long-term score history" points={pts.map((p) => ({ date: p.run_date, value: p.long_term_score }))} w={300} h={50} /> : <p className="muted">History builds up as scores are recalculated.</p>}
              <p><Link href={`/stocks/${encodeURIComponent(stock.isin)}`}>Open the stock page</Link></p>
            </>
          ) : <p className="muted">No score recorded for this company yet.</p>}
          {detail.rerunnable ? <button type="button" className="btn" disabled={busy} onClick={rerun} aria-label={`Re-run ${node.name}`}>Re-run</button> : null}
          {msg ? (msg.ok ? <p role="status" className="msg-ok">{msg.text}</p> : <p role="alert" className="error">{msg.text}</p>) : null}
        </div>
        <div className="panel">
          <h2>Recent activity</h2>
          {feed.length === 0 ? <p className="muted">No recent events for this company.</p> : (
            <ol className="trace">
              {feed.map((e) => (
                <li key={e.id} className={`trace-${e.kind}`}><b>{e.kind}</b> {name(e.agent_id)}
                  <span className="muted"> · {e.created_at ? new Date(e.created_at).toLocaleTimeString() : ""}</span></li>
              ))}
            </ol>
          )}
        </div>
      </div>
    </section>
  );
}
