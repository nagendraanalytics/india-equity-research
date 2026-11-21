"use client";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { ApiError, apiGet, apiPost } from "../lib/api";
import { tierLabel } from "../lib/tree";
import type { MetricsResponse, RerunResponse, Summary, TreeNode, TreeResponse } from "../lib/types";
import { Columns, Donut, FleetGrid, HBars, StackedArea, StackedBars } from "./charts2";
import { ErrorBox, Loading } from "./ui";

export const FLEET_POLL_MS = 5_000;
export const STATUS_COLORS: Record<string, string> = {
  running: "#38d4e8", completed: "#5fc38f", failed: "#ff7a6b", degraded: "#f0b84e",
  blocked: "#b08a3a", queued: "#b4aaff", idle: "#3b5266",
};
const STATUS_ORDER = Object.keys(STATUS_COLORS);
const ROW_H = 34, VIEW_H = 380;

export interface FlatAgent { id: number; name: string; tier: number; status: string; task: string | null; lastRun: string | null; cost: number; parent: string }

export function flatten(root: TreeNode): FlatAgent[] {
  const out: FlatAgent[] = [];
  const walk = (n: TreeNode, parent: string) => {
    out.push({ id: n.id, name: n.name, tier: n.tier, status: n.status, task: n.current_task, lastRun: n.last_run, cost: n.cost_today, parent });
    for (const c of n.children) walk(c, n.name);
  };
  walk(root, "");
  return out;
}

const hhmm = (iso: string) => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

export function FleetView({ pollMs = FLEET_POLL_MS }: { pollMs?: number }) {
  const [tree, setTree] = useState<TreeNode | null>(null);
  const [metrics, setMetrics] = useState<MetricsResponse | null>(null);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [metricsErr, setMetricsErr] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loadedAt, setLoadedAt] = useState<Date | null>(null);
  const [tier, setTier] = useState(0);
  const [status, setStatus] = useState("");
  const [q, setQ] = useState("");
  const [sel, setSel] = useState<number | null>(null);
  const [scroll, setScroll] = useState(0);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const inflight = useRef(false);

  useEffect(() => {
    let dead = false;
    const load = async () => {
      try {
        const [t, s] = await Promise.all([apiGet<TreeResponse>("/agents/tree"), apiGet<Summary>("/agents/summary")]);
        if (dead) return;
        setTree(t.tree); setSummary(s); setError(null); setLoadedAt(new Date());
        // charts are optional: an API that predates /agents/metrics must not blank the whole page
        try { const m = await apiGet<MetricsResponse>("/agents/metrics"); if (!dead) { setMetrics(m); setMetricsErr(null); } }
        catch (e) { if (!dead) setMetricsErr(e instanceof Error ? e.message : String(e)); }
      } catch (e) {
        if (!dead) setError(e instanceof Error ? e.message : String(e));
      }
    };
    void load();
    const timer = setInterval(load, pollMs);
    return () => { dead = true; clearInterval(timer); };
  }, [pollMs]);

  const all = useMemo(() => (tree ? flatten(tree) : []), [tree]);
  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return all.filter((a) => (!tier || a.tier === tier) && (!status || a.status === status)
      && (!needle || `${a.name} ${a.task ?? ""}`.toLowerCase().includes(needle)));
  }, [all, tier, status, q]);

  const rerun = useCallback(async (id: number) => {
    if (inflight.current) return;
    inflight.current = true; setBusy(true); setMsg(null);
    try {
      const r = await apiPost<RerunResponse>(`/agents/${id}/rerun`);
      setMsg({ ok: true, text: r.already_queued ? `Re-run already queued (job ${r.job_id})` : `Re-run queued (job ${r.job_id})` });
    } catch (e) {
      const st = e instanceof ApiError ? e.status : 0;
      setMsg({ ok: false, text: `Re-run failed${st ? ` (${st})` : ""}: ${e instanceof Error ? e.message : String(e)}` });
    } finally { inflight.current = false; setBusy(false); }
  }, []);

  if (error && !tree) return <ErrorBox message={error} />;
  if (!tree || (!metrics && !metricsErr)) return <Loading what="agent fleet" />;

  const count = (s: string) => all.filter((a) => a.status === s).length;
  const tiers = [...new Set(all.map((a) => a.tier))].sort((a, b) => a - b);
  const byStatus = STATUS_ORDER.map((s) => ({ label: s, value: count(s), color: STATUS_COLORS[s] }));
  const byTier = tiers.map((t) => ({ label: tierLabel(t), parts: Object.fromEntries(STATUS_ORDER.map((s) => [s, all.filter((a) => a.tier === t && a.status === s).length])) }));
  const costly = [...all].sort((a, b) => b.cost - a.cost).filter((a) => a.cost > 0).slice(0, 10)
    .map((a) => ({ label: a.name, value: a.cost }));
  const jobs = Object.entries(metrics?.jobs_by_status ?? {}).map(([k, v]) => ({ label: k, value: v }));
  const picked = sel !== null ? all.find((a) => a.id === sel) ?? null : null;

  const first = Math.max(0, Math.floor(scroll / ROW_H) - 4);
  const last = Math.min(rows.length, first + Math.ceil(VIEW_H / ROW_H) + 8);
  const shown = rows.slice(first, last);

  return (
    <section>
      <h1>Agent fleet</h1>
      <p className="freshness" data-testid="fleet-fresh">
        {loadedAt ? `Updated ${loadedAt.toLocaleTimeString()} · refreshes every ${Math.round(pollMs / 1000)} s` : "Loading"}
        {error ? <span className="error" role="alert"> Refresh failed: {error}</span> : null}
      </p>

      <div className="tiles" aria-label="Fleet summary">
        <div className="tile"><b data-testid="fleet-total">{all.length}</b><span>agents</span></div>
        <div className="tile tile-run"><b data-testid="fleet-running">{count("running")}</b><span>running now</span></div>
        <div className="tile tile-ok"><b>{count("completed")}</b><span>completed</span></div>
        <div className={`tile${count("failed") + count("degraded") > 0 ? " tile-bad" : ""}`}><b data-testid="fleet-failed">{count("failed") + count("degraded")}</b><span>failed or degraded</span></div>
        <div className="tile"><b>{summary?.queue_depth ?? "—"}</b><span>jobs waiting</span></div>
        <div className="tile"><b>{summary ? summary.compute_tokens_today.toLocaleString() : "—"}</b><span>tokens today</span></div>
      </div>

      <div className="grid3">
        <div className="panel"><h2>Status mix</h2><Donut title="Agents by status" slices={byStatus} /></div>
        <div className="panel"><h2>Status by tier</h2><StackedBars title="Agents by tier and status" rows={byTier} colors={STATUS_COLORS} /></div>
        {metrics ? (<>
        <div className="panel"><h2>Job queue</h2><HBars title="Jobs by status" rows={jobs} /></div>
        <div className="panel"><h2>Activity, last 60 minutes</h2>
          <StackedArea title="Events per minute" labels={metrics.events_by_minute.map((p) => hhmm(p.t))}
            series={[{ key: "started", color: STATUS_COLORS.running, values: metrics.events_by_minute.map((p) => p.started) },
                     { key: "completed", color: STATUS_COLORS.completed, values: metrics.events_by_minute.map((p) => p.completed) },
                     { key: "failed", color: STATUS_COLORS.failed, values: metrics.events_by_minute.map((p) => p.failed) }]} /></div>
        <div className="panel"><h2>Tokens, last 24 hours</h2>
          <Columns title="Tokens per hour" labels={metrics.tokens_by_hour.map((p) => hhmm(p.t))} values={metrics.tokens_by_hour.map((p) => p.tokens)} unit=" tokens" /></div>
        </>) : (
          <div className="panel"><h2>Activity charts unavailable</h2>
            <p className="error" role="alert">The API did not return /agents/metrics ({metricsErr}). Restart the API from the latest code.</p></div>
        )}
        <div className="panel"><h2>Highest cost today</h2><HBars title="Cost today, top 10 agents" rows={costly} color="#b4aaff" /></div>
      </div>

      <h2>Fleet map</h2>
      <div className="fleet-toolbar" role="search">
        <label>Search <input value={q} onChange={(e) => { setQ(e.target.value); setScroll(0); }} placeholder="agent or task" /></label>
        <label>Tier <select value={tier} onChange={(e) => { setTier(Number(e.target.value)); setScroll(0); }}>
          <option value={0}>All tiers</option>{tiers.map((t) => <option key={t} value={t}>{tierLabel(t)}</option>)}</select></label>
        <label>Status <select value={status} onChange={(e) => { setStatus(e.target.value); setScroll(0); }}>
          <option value="">All statuses</option>{STATUS_ORDER.map((s) => <option key={s} value={s}>{s}</option>)}</select></label>
        <span data-testid="fleet-matching">{rows.length} of {all.length} agents</span>
      </div>
      <div className="panel"><FleetGrid cells={rows} colors={STATUS_COLORS} selectedId={sel} onPick={setSel} /></div>

      <div className="fleet-body">
        <div className="vlist" role="list" aria-label="Agents" onScroll={(e) => setScroll(e.currentTarget.scrollTop)} data-testid="fleet-list">
          <div className="vrow vhead" role="presentation"><span>ID</span><span>Agent</span><span>Tier</span><span>Status</span><span>Cost</span></div>
          <div style={{ height: first * ROW_H }} />
          {shown.map((a) => (
            <div key={a.id} role="listitem" className={`vrow${a.id === sel ? " sel" : ""}`} onClick={() => setSel(a.id)} data-testid={`fleet-row-${a.id}`}>
              <span className="muted">#{a.id}</span><span title={a.name}>{a.name}</span><span className="muted">{tierLabel(a.tier)}</span>
              <span><span className={`badge status status-${a.status}`}>{a.status}</span></span><span>{a.cost.toFixed(2)}</span>
            </div>
          ))}
          <div style={{ height: Math.max(0, rows.length - last) * ROW_H }} />
        </div>

        <aside className="panel drawer" aria-label="Selected agent">
          {picked ? (
            <>
              <h2>{picked.name}</h2>
              <dl>
                <dt>Tier</dt><dd>{tierLabel(picked.tier)}</dd>
                <dt>Status</dt><dd><span className={`badge status status-${picked.status}`}>{picked.status}</span></dd>
                <dt>Task</dt><dd>{picked.task ?? "none"}</dd>
                <dt>Parent</dt><dd>{picked.parent || "none"}</dd>
                <dt>Cost today</dt><dd>{picked.cost.toFixed(2)}</dd>
                <dt>Last run</dt><dd>{picked.lastRun ?? "never"}</dd>
              </dl>
              {picked.tier === 5 ? <p><Link href={`/company/${picked.id}`}>Open company agents</Link></p> : null}
              <button type="button" className="btn" disabled={busy} onClick={() => rerun(picked.id)} aria-label={`Re-run ${picked.name}`}>Re-run</button>
              {msg ? (msg.ok ? <p role="status" className="msg-ok">{msg.text}</p> : <p role="alert" className="error">{msg.text}</p>) : null}
            </>
          ) : <p className="muted">Select a square on the map or a row in the list to see its details.</p>}
          <h3>Recent failures</h3>
          {!metrics || metrics.recent_failures.length === 0 ? <p className="muted">None recorded.</p> : (
            <ul>{(metrics?.recent_failures ?? []).slice(0, 5).map((f) => (
              <li key={f.event_id}><button type="button" className="link" onClick={() => setSel(f.agent_id)}>agent {f.agent_id}</button>: {f.error ?? "no message"}</li>
            ))}</ul>
          )}
        </aside>
      </div>
    </section>
  );
}
