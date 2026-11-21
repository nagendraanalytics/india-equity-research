"use client";
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";

import { apiGet } from "../lib/api";
import { tierLabel } from "../lib/tree";
import type { AgentEvent, TreeNode, TreeResponse } from "../lib/types";
import { FlatAgent, STATUS_COLORS, flatten } from "./FleetView";
import { ErrorBox, Loading } from "./ui";

export const PIPELINE_POLL_MS = 4_000;

export interface Run { agentId: number; start: number; end: number | null; outcome: "completed" | "failed" | "running" }

/** Pair each `started` with the next `completed`/`failed` of the same agent (events in any order). */
export function buildRuns(events: AgentEvent[]): Run[] {
  const byAgent = new Map<number, AgentEvent[]>();
  for (const e of events) {
    if (!e.created_at) continue;
    (byAgent.get(e.agent_id) ?? byAgent.set(e.agent_id, []).get(e.agent_id)!).push(e);
  }
  const runs: Run[] = [];
  for (const [agentId, list] of byAgent) {
    list.sort((a, b) => a.id - b.id);
    let open: number | null = null;
    for (const e of list) {
      const t = Date.parse(e.created_at as string);
      if (e.kind === "started") {
        if (open !== null) runs.push({ agentId, start: open, end: null, outcome: "running" });
        open = t;
      } else if ((e.kind === "completed" || e.kind === "failed") && open !== null) {
        runs.push({ agentId, start: open, end: t, outcome: e.kind });
        open = null;
      }
    }
    if (open !== null) runs.push({ agentId, start: open, end: null, outcome: "running" });
  }
  return runs;
}

const WINDOWS: [string, number][] = [["15 minutes", 15], ["1 hour", 60], ["6 hours", 360], ["24 hours", 1440], ["7 days", 10080]];
const fmtDur = (ms: number) => (ms < 1000 ? "<1 s" : ms < 60_000 ? `${Math.round(ms / 1000)} s` : `${(ms / 60_000).toFixed(1)} min`);
const clock = (t: number) => new Date(t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

function companyWorkers(root: TreeNode) {
  const out: { company: TreeNode; workers: TreeNode[] }[] = [];
  const walk = (n: TreeNode) => {
    if (n.tier === 5) out.push({ company: n, workers: n.children });
    else n.children.forEach(walk);
  };
  walk(root);
  return out;
}

export function PipelineView({ pollMs = PIPELINE_POLL_MS }: { pollMs?: number }) {
  const [tree, setTree] = useState<TreeNode | null>(null);
  const [events, setEvents] = useState<AgentEvent[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const [minutes, setMinutes] = useState(360);
  const [tier, setTier] = useState(0);
  const [sel, setSel] = useState<number | null>(null);

  useEffect(() => {
    let dead = false;
    const load = async () => {
      try {
        const [t, a] = await Promise.all([apiGet<TreeResponse>("/agents/tree"),
          apiGet<{ events: AgentEvent[] }>("/agents/activity", { limit: "800" })]);
        if (dead) return;
        setTree(t.tree); setEvents(a.events); setNow(Date.now()); setError(null);
      } catch (e) { if (!dead) setError(e instanceof Error ? e.message : String(e)); }
    };
    void load();
    const timer = setInterval(load, pollMs);
    return () => { dead = true; clearInterval(timer); };
  }, [pollMs]);

  const agents = useMemo(() => (tree ? flatten(tree) : []), [tree]);
  const byId = useMemo(() => new Map(agents.map((a) => [a.id, a])), [agents]);
  const runs = useMemo(() => buildRuns(events), [events]);

  if (error && !tree) return <ErrorBox message={error} />;
  if (!tree) return <Loading what="pipeline" />;

  const from = now - minutes * 60_000;
  const visible = runs.filter((r) => (r.end ?? now) >= from && (!tier || byId.get(r.agentId)?.tier === tier));
  const lanes = new Map<number, Run[]>();
  for (const r of visible) (lanes.get(r.agentId) ?? lanes.set(r.agentId, []).get(r.agentId)!).push(r);
  const laneIds = [...lanes.keys()].sort((a, b) =>
    Math.max(...lanes.get(b)!.map((r) => r.start)) - Math.max(...lanes.get(a)!.map((r) => r.start))).slice(0, 25);

  const tiers = [...new Set(agents.map((a) => a.tier))].sort((a, b) => a - b);
  const stage = (t: number) => {
    const xs = agents.filter((a) => a.tier === t);
    const n = (s: string) => xs.filter((a) => a.status === s).length;
    return { t, total: xs.length, running: n("running"), failed: n("failed") + n("degraded"), done: n("completed") };
  };

  // Gantt geometry
  const W = 900, labelW = 210, rowH = 24, top = 22;
  const x = (t: number) => labelW + ((Math.min(Math.max(t, from), now) - from) / (now - from)) * (W - labelW - 10);
  const H = top + Math.max(1, laneIds.length) * rowH + 4;
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => from + f * (now - from));

  const picked: FlatAgent | undefined = sel !== null ? byId.get(sel) : undefined;
  const trace = sel !== null ? events.filter((e) => e.agent_id === sel).sort((a, b) => a.id - b.id) : [];
  const pipelines = companyWorkers(tree);

  return (
    <section>
      <h1>Agent pipeline</h1>
      <p className="note">How work flows from the master agent down to the workers that read filings, score and verify. Updates every {Math.round(pollMs / 1000)} s.
        {error ? <span className="error" role="alert"> Refresh failed: {error}</span> : null}</p>

      <ol className="stages" aria-label="Pipeline stages">
        {tiers.map((t) => { const s = stage(t); return (
          <li key={t} className={`stage${tier === t ? " on" : ""}`}>
            <button type="button" onClick={() => setTier(tier === t ? 0 : t)} aria-pressed={tier === t} aria-label={`${tierLabel(t)} stage, ${s.total} agents`}>
              <span className="stage-name">{tierLabel(t)}</span>
              <b>{s.total}</b>
              <span className="stage-bar" aria-hidden="true">
                <i style={{ flex: s.done, background: STATUS_COLORS.completed }} /><i style={{ flex: s.running, background: STATUS_COLORS.running }} />
                <i style={{ flex: s.failed, background: STATUS_COLORS.failed }} /><i style={{ flex: Math.max(0, s.total - s.done - s.running - s.failed), background: STATUS_COLORS.idle }} />
              </span>
              <span className="stage-note">{s.running} running · {s.failed} failed</span>
            </button>
          </li>); })}
      </ol>

      <div className="fleet-toolbar">
        <label>Time window <select value={minutes} onChange={(e) => setMinutes(Number(e.target.value))}>
          {WINDOWS.map(([l, m]) => <option key={m} value={m}>Last {l}</option>)}</select></label>
        <span>{tier ? `Showing the ${tierLabel(tier)} stage` : "Showing all stages"} · {laneIds.length} agents with activity</span>
      </div>

      <div className="panel">
        <h2>Activity timeline</h2>
        {laneIds.length === 0 ? <p className="empty">No agent ran in this window. Widen the time window or drop a file to start a run.</p> : (
          <figure className="chart">
            <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`Run timeline for ${laneIds.length} agents`}>
              {ticks.map((t, i) => <g key={i}><line x1={x(t)} x2={x(t)} y1={top - 4} y2={H} className="c-grid" />
                <text x={x(t)} y={12} fontSize="10" textAnchor={i === 0 ? "start" : i === 4 ? "end" : "middle"} className="c-dim">{clock(t)}</text></g>)}
              {laneIds.map((id, i) => {
                const a = byId.get(id);
                return (
                  <g key={id} transform={`translate(0 ${top + i * rowH})`} className="lane" onClick={() => setSel(id)} style={{ cursor: "pointer" }}>
                    <rect x={0} y={0} width={W} height={rowH} fill={id === sel ? "rgba(56,212,232,.1)" : "transparent"} />
                    <text x={labelW - 8} y={16} textAnchor="end" fontSize="11" className="c-ink">{(a?.name ?? `agent ${id}`).slice(0, 26)}</text>
                    {lanes.get(id)!.map((r, k) => (
                      <rect key={k} x={x(r.start)} y={4} width={Math.max(4, x(r.end ?? now) - x(r.start))} height={rowH - 9} rx={3}
                            fill={STATUS_COLORS[r.outcome]}><title>{`${a?.name ?? id}: ${r.outcome}, ${fmtDur((r.end ?? now) - r.start)} from ${clock(r.start)}`}</title></rect>
                    ))}
                  </g>
                );
              })}
            </svg>
            <figcaption className="legend">{(["completed", "running", "failed"] as const).map((k) => <span key={k}><i style={{ background: STATUS_COLORS[k] }} />{k}</span>)}</figcaption>
          </figure>
        )}
      </div>

      <div className="fleet-body">
        <div className="panel">
          <h2>Company pipelines</h2>
          {pipelines.length === 0 ? <p className="empty">No company agents yet.</p> : (
            <table>
              <thead><tr><th>Company</th><th>Steps</th><th>Cost today</th></tr></thead>
              <tbody>
                {pipelines.slice(0, 100).map(({ company, workers }) => (
                  <tr key={company.id} onClick={() => setSel(company.id)} style={{ cursor: "pointer" }}>
                    <td><Link href={`/company/${company.id}`}>{company.name}</Link> <span className={`badge status status-${company.status}`}>{company.status}</span></td>
                    <td>{workers.length === 0 ? <span className="muted">no workers</span> : workers.map((w, i) => (
                      <span key={w.id} className="step">{i > 0 ? <span className="arrow" aria-hidden="true">→</span> : null}
                        <span className={`badge status status-${w.status}`} title={w.current_task ?? undefined}>{w.name}</span></span>))}</td>
                    <td>{(company.cost_today + workers.reduce((a, w) => a + w.cost_today, 0)).toFixed(2)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        <aside className="panel drawer" aria-label="Agent trace">
          <h2>{picked ? picked.name : "Agent trace"}</h2>
          {!picked ? <p className="muted">Select a row in the timeline or a company to see its recent steps.</p> : trace.length === 0 ? <p className="muted">No recent events for this agent.</p> : (
            <ol className="trace">
              {trace.slice(-12).map((e, i, arr) => {
                const prev = arr[i - 1];
                const gap = prev && prev.created_at && e.created_at ? Date.parse(e.created_at) - Date.parse(prev.created_at) : null;
                return <li key={e.id} className={`trace-${e.kind}`}><b>{e.kind}</b> {e.created_at ? clock(Date.parse(e.created_at)) : ""}
                  {gap !== null ? <span className="muted"> · +{fmtDur(gap)}</span> : null}</li>;
              })}
            </ol>
          )}
        </aside>
      </div>
    </section>
  );
}
