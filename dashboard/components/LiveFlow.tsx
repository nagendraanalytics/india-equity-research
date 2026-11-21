"use client";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { useLiveAgents } from "../lib/useLiveAgents";
import type { AgentEvent, TreeNode } from "../lib/types";

const TIER = { master: 1, company: 5, segment: 7, source: 8 };
const ACTIVE_MS = 4_000;
const W = 920;
const X = { source: 150, segment: 400, master: 600, company: 820 };

function flatten(n: TreeNode, parent: TreeNode | null, out: Array<{ n: TreeNode; parent: TreeNode | null }> = []) {
  out.push({ n, parent });
  n.children.forEach((c) => flatten(c, n, out));
  return out;
}
const clip = (s: string, k: number) => (s.length > k ? s.slice(0, k - 1) + "…" : s);
const label = (s: string) => s.replace(/_/g, " ");
const stateOf = (s: string) => (["running", "completed", "failed", "queued", "blocked", "degraded"].includes(s) ? s : "idle");

/** Live picture of the agents: sources feed segment controllers, which report to the master, which
 *  feeds the company analyses. Nodes pulse when an event arrives; lines flow while work is running. */
export function LiveFlow() {
  const [pings, setPings] = useState<Record<number, number>>({});
  const [now, setNow] = useState(() => Date.now());
  const parents = useRef<Map<number, number | null>>(new Map());

  const onEvent = useCallback((ev: AgentEvent) => {
    const t = Date.now();
    setPings((p) => {
      const next = { ...p };
      let id: number | null | undefined = ev.agent_id;
      for (let i = 0; id != null && i < 10; i++) { next[id] = t; id = parents.current.get(id); }
      return next;
    });
  }, []);
  const { root, feed, live, error } = useLiveAgents(onEvent);

  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);

  const model = useMemo(() => {
    if (!root) return null;
    const all = flatten(root, null);
    parents.current = new Map(all.map(({ n, parent }) => [n.id, parent ? parent.id : null]));
    const names = new Map(all.map(({ n }) => [n.id, n.name]));
    const tier = (t: number) => all.filter(({ n }) => n.tier === t);
    const sources = tier(TIER.source), segments = tier(TIER.segment);
    const companies = tier(TIER.company).slice(0, 8);
    return { all, names, sources, segments, companies, hiddenCompanies: Math.max(0, tier(TIER.company).length - 8) };
  }, [root]);

  if (!model) {
    return (
      <section className="panel live" aria-label="Live agent activity">
        <h2>Live agents</h2>
        <p className="note">{error ? `Live view unavailable: ${error}` : "Connecting…"}</p>
      </section>
    );
  }

  const { all, names, sources, segments, companies } = model;
  const rowH = 24, top = 30;
  const rows = Math.max(sources.length, companies.length, segments.length, 4);
  const H = top + rows * rowH + 20;
  const hot = (id: number) => now - (pings[id] ?? 0) < ACTIVE_MS;
  const isActive = (n: TreeNode) => n.status === "running" || hot(n.id);

  const pos = new Map<number, { x: number; y: number }>();
  sources.forEach(({ n }, i) => pos.set(n.id, { x: X.source, y: top + i * rowH + 10 }));
  segments.forEach(({ n }) => {
    const ys = n.children.map((c) => pos.get(c.id)?.y).filter((y): y is number => y !== undefined);
    pos.set(n.id, { x: X.segment, y: ys.length ? ys.reduce((a, b) => a + b, 0) / ys.length : top });
  });
  const masterY = top + (rows * rowH) / 2;
  const masterId = root!.id;
  pos.set(masterId, { x: X.master, y: masterY });
  const span = Math.max(companies.length - 1, 1) * rowH * 1.4;
  companies.forEach(({ n }, i) => pos.set(n.id, { x: X.company, y: companies.length === 1 ? masterY : masterY - span / 2 + (i * span) / (companies.length - 1) }));

  const edge = (a: TreeNode, b: TreeNode, key: string) => {
    const p = pos.get(a.id), q = pos.get(b.id);
    if (!p || !q) return null;
    const mx = (p.x + q.x) / 2;
    const active = isActive(a) || isActive(b);
    return <path key={key} d={`M${p.x} ${p.y} C${mx} ${p.y} ${mx} ${q.y} ${q.x} ${q.y}`} className={`lf-edge${active ? " on" : ""}`} />;
  };
  const counts = { running: 0, queued: 0, failed: 0 } as Record<string, number>;
  for (const { n } of all) if (n.status in counts) counts[n.status] += 1;

  return (
    <section className="panel live" aria-label="Live agent activity">
      <div className="live-head">
        <h2>Live agents</h2>
        <span className={`live-pill live-${live}`}><i className="dot-live-i" />{live === "live" ? "streaming" : live === "unavailable" ? "polling" : live}</span>
        <span className="live-counts" data-testid="live-counts">{counts.running} running · {counts.queued} queued · {counts.failed} failed</span>
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`Agent flow: ${sources.length} sources feed ${segments.length} segment controllers, which report to the master, which feeds ${companies.length} company analyses`} className="lf">
        <g className="lf-cols">
          {([["Sources", X.source], ["Segment controllers", X.segment], ["Master", X.master], ["Company analysis", X.company]] as const).map(([t, x]) => (
            <text key={t} x={x} y={12} textAnchor="middle" className="lf-col">{t}</text>
          ))}
        </g>
        {segments.map(({ n }) => n.children.map((c) => edge(c, n, `s${c.id}`)))}
        {segments.map(({ n }) => edge(n, root!, `m${n.id}`))}
        {companies.map(({ n }) => edge(root!, n, `c${n.id}`))}
        {sources.map(({ n }) => {
          const p = pos.get(n.id)!;
          return (
            <g key={n.id} transform={`translate(${p.x} ${p.y})`} className={`lf-node lf-${stateOf(n.status)}${hot(n.id) ? " hot" : ""}`} data-testid="lf-source">
              <title>{`${label(n.name)}: ${n.status}`}</title>
              <rect x={-110} y={-9} width={220} height={18} rx={9} />
              <circle cx={-98} cy={0} r={4} />
              <text x={-88} y={4}>{clip(label(n.name), 30)}</text>
              {hot(n.id) ? <circle key={pings[n.id]} cx={-98} cy={0} r={4} className="lf-ring" /> : null}
            </g>
          );
        })}
        {segments.map(({ n }) => {
          const p = pos.get(n.id)!;
          return (
            <g key={n.id} transform={`translate(${p.x} ${p.y})`} className={`lf-node lf-seg lf-${stateOf(n.status)}${hot(n.id) ? " hot" : ""}`} data-testid="lf-segment">
              <title>{`${label(n.name)} controller: ${n.status}`}</title>
              <rect x={-70} y={-12} width={140} height={24} rx={7} />
              <text x={0} y={4} textAnchor="middle">{clip(label(n.name), 18)}</text>
              {hot(n.id) ? <rect key={pings[n.id]} x={-70} y={-12} width={140} height={24} rx={7} className="lf-ring-r" /> : null}
            </g>
          );
        })}
        <g transform={`translate(${X.master} ${masterY})`} className={`lf-node lf-master lf-${stateOf(root!.status)}${hot(masterId) ? " hot" : ""}`}>
          <title>{`master: ${root!.status}`}</title>
          <circle r={26} />
          <text y={4} textAnchor="middle">master</text>
          {hot(masterId) ? <circle key={pings[masterId]} r={26} className="lf-ring" /> : null}
        </g>
        {companies.map(({ n }) => {
          const p = pos.get(n.id)!;
          return (
            <g key={n.id} transform={`translate(${p.x} ${p.y})`} className={`lf-node lf-seg lf-${stateOf(n.status)}${hot(n.id) ? " hot" : ""}`} data-testid="lf-company">
              <title>{`${n.name}: ${n.status}`}</title>
              <rect x={-80} y={-12} width={160} height={24} rx={7} />
              <text x={0} y={4} textAnchor="middle">{clip(n.name, 22)}</text>
            </g>
          );
        })}
        {model.hiddenCompanies ? <text x={X.company} y={H - 4} textAnchor="middle" className="lf-col">+{model.hiddenCompanies} more companies</text> : null}
      </svg>
      <ol className="live-feed" aria-label="Latest agent events">
        {feed.length === 0 ? <li className="muted">Waiting for activity. Events appear here as agents start, finish or fail.</li> : feed.slice(0, 5).map((e) => (
          <li key={e.id} className={`lf-ev lf-ev-${e.kind}`}>
            <time>{e.created_at ? new Date(e.created_at).toLocaleTimeString() : ""}</time>
            <b>{label(names.get(e.agent_id) ?? `agent ${e.agent_id}`)}</b> {e.kind}
          </li>
        ))}
      </ol>
    </section>
  );
}
