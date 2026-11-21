"use client";
import { useCallback, useEffect, useRef, useState } from "react";

import { API_BASE, ApiError, apiGet, apiPost, buildUrl } from "../lib/api";
import { applyEvent, resolveFocus, tierLabel } from "../lib/tree";
import type { AgentEvent, RerunResponse, Summary, TreeNode, TreeResponse } from "../lib/types";
import { Empty, ErrorBox, Loading } from "./ui";

export const RESYNC_MS = 30_000;
export const RETRY_MS = 1_000;
/** Children drawn at once; a sector can hold hundreds of company agents. */
export const CHILD_PAGE = 60;

type Msg = { ok: boolean; text: string };

const STATUS_HELP: Record<string, string> = {
  blocked: "queued, waiting for its parent job (sector or industry) to finish",
  degraded: "a sub-agent failed",
};

function StatusBadge({ id, status }: { id: number; status: string }) {
  return <span data-testid={`status-${id}`} className={`badge status status-${status} cx-badge cx-b-${status}`}
               title={STATUS_HELP[status]}>{status}</span>;
}

function parseEvent(raw: unknown): AgentEvent | null {
  try {
    const d = JSON.parse(String((raw as MessageEvent).data));
    if (typeof d?.id !== "number" || typeof d?.agent_id !== "number" || typeof d?.kind !== "string") return null;
    return d as AgentEvent;
  } catch {
    return null;
  }
}

/**
 * Agent console. Data flow (backend contract, see ier.api.main docstring): the stream is only a
 * latency optimisation. On every (re)connect and every RESYNC_MS the tree is reloaded FIRST,
 * then /stream is opened at that tree's last_event_id, and only events with a greater id are
 * applied (deduplicated by id; events are idempotent). The browser EventSource cannot set a
 * request header on the first connect, so the id travels in the supported `?last_event_id=`.
 */
export function ConsoleView({ resyncMs = RESYNC_MS, retryMs = RETRY_MS }: { resyncMs?: number; retryMs?: number }) {
  const [root, setRoot] = useState<TreeNode | null>(null);
  const [lastId, setLastId] = useState<number | null>(null);
  const [loadedAt, setLoadedAt] = useState<Date | null>(null);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [summaryErr, setSummaryErr] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [live, setLive] = useState<"connecting" | "live" | "reconnecting">("connecting");
  const [path, setPath] = useState<number[]>([]);
  const [msgs, setMsgs] = useState<Record<number, Msg>>({});
  const [busy, setBusy] = useState<Set<number>>(new Set());
  const [limit, setLimit] = useState(CHILD_PAGE);
  const [feed, setFeed] = useState<AgentEvent[]>([]);
  const inflight = useRef<Set<number>>(new Set());

  useEffect(() => {
    let disposed = false;
    let gen = 0;
    let es: EventSource | null = null;
    let retryTimer: ReturnType<typeof setTimeout> | undefined;
    const applied = new Set<number>();

    const closeStream = () => {
      if (es) { es.close(); es = null; }
    };
    const scheduleRetry = (ms: number) => {
      clearTimeout(retryTimer);
      retryTimer = setTimeout(resync, ms);
    };
    const loadSummary = () =>
      apiGet<Summary>("/agents/summary").then(
        (s) => { if (!disposed) { setSummary(s); setSummaryErr(null); } },
        (e) => { if (!disposed) setSummaryErr(e instanceof Error ? e.message : String(e)); },
      );

    async function resync() {
      const mine = ++gen;
      clearTimeout(retryTimer);
      closeStream();                      // nothing from the old connection may apply any more
      setLive("connecting");
      let t: TreeResponse;
      try {
        t = await apiGet<TreeResponse>("/agents/tree");
      } catch (e) {
        if (disposed || mine !== gen) return;
        setError(e instanceof Error ? e.message : String(e));
        setLive("reconnecting");
        scheduleRetry(retryMs * 5);
        return;
      }
      if (disposed || mine !== gen) return;
      void loadSummary();
      setError(null);
      setRoot(t.tree);
      // keep only the part of the drill path that still exists in the new tree
      setPath((p) => resolveFocus(t.tree, p).slice(1).map((n) => n.id));
      setLastId(t.last_event_id);
      setLoadedAt(new Date());
      applied.clear();
      const floor = t.last_event_id;
      // tree is in state; only now open the stream
      const source = new EventSource(buildUrl("/stream", { last_event_id: String(floor) }));
      es = source;
      source.onopen = () => { if (es === source && !disposed) setLive("live"); };
      source.addEventListener("agent_event", (raw) => {
        if (es !== source || disposed) return;
        const ev = parseEvent(raw);
        if (!ev || ev.id <= floor || applied.has(ev.id)) return;
        applied.add(ev.id);
        setFeed((f) => [ev, ...f].slice(0, 12));
        setRoot((prev) => (prev ? applyEvent(prev, ev) : prev));
      });
      source.onerror = () => {
        if (es !== source || disposed) return;
        closeStream();
        setLive("reconnecting");
        scheduleRetry(retryMs);           // reconnect = tree first, then a new stream
      };
    }

    void resync();
    const timer = setInterval(resync, resyncMs);
    return () => {
      disposed = true;
      clearInterval(timer);
      clearTimeout(retryTimer);
      closeStream();
    };
  }, [resyncMs, retryMs]);

  const rerun = useCallback(async (id: number) => {
    if (inflight.current.has(id)) return;        // one POST per node at a time
    inflight.current.add(id);
    setBusy(new Set(inflight.current));
    setMsgs((m) => { const { [id]: _old, ...rest } = m; return rest; });
    try {
      const r = await apiPost<RerunResponse>(`/agents/${id}/rerun`);
      setMsgs((m) => ({ ...m, [id]: { ok: true,
        text: r.already_queued ? `Re-run already queued (job ${r.job_id})` : `Re-run queued (job ${r.job_id})` } }));
    } catch (e) {
      const status = e instanceof ApiError ? e.status : 0;
      setMsgs((m) => ({ ...m, [id]: { ok: false,
        text: `Re-run failed${status ? ` (${status})` : ""}: ${e instanceof Error ? e.message : String(e)}` } }));
    } finally {
      inflight.current.delete(id);
      setBusy(new Set(inflight.current));
    }
  }, []);

  const chain = root ? resolveFocus(root, path) : [];
  const focus = chain.length ? chain[chain.length - 1] : null;
  const focusId = focus?.id;
  useEffect(() => { setLimit(CHILD_PAGE); }, [focusId]);

  const rerunUi = (n: TreeNode) => (
    <>
      {n.tier > 1 ? (   // the master has nothing to run: the API refuses it
        <button type="button" className="btn" aria-label={`Re-run ${n.name}`} disabled={busy.has(n.id)} onClick={() => rerun(n.id)}>Re-run</button>
      ) : null}
      {msgs[n.id] ? (
        msgs[n.id].ok
          ? <p role="status" className="msg-ok">{msgs[n.id].text}</p>
          : <p role="alert" className="error">{msgs[n.id].text}</p>
      ) : null}
    </>
  );

  return (
    <section className="cx">
      <h1>Agent console</h1>
      <div className="cx-kpis" aria-label="Run summary">
        <div className="cx-kpi"><span className="k">Queue depth</span><b data-testid="queue-depth">{summary ? summary.queue_depth : "—"}</b></div>
        <div className="cx-kpi cx-kpi-wide"><span className="k">Compute used today</span>
          <b data-testid="compute-today">{summary ? `${summary.compute_tokens_today} tokens · ${summary.compute_model_seconds_today.toFixed(1)} model-seconds` : "—"}</b></div>
        <div className={`cx-kpi${summary && summary.failures_today > 0 ? " cx-kpi-warn" : ""}`}><span className="k">Failures today</span><b data-testid="failures-today">{summary ? summary.failures_today : "—"}</b></div>
        {summaryErr ? <div className="error" role="alert">Run summary unavailable: {summaryErr}</div> : null}
      </div>
      <p data-testid="freshness" className="freshness cx-fresh"><span className={`cx-dot cx-dot-${live}`} aria-hidden="true" />
        {loadedAt && lastId !== null
          ? <>Tree loaded {loadedAt.toLocaleTimeString()} at event #{lastId} · stream: {live}</>
          : <>No data yet · stream: {live}</>}
        {focus ? <> · last run of selected node: {focus.last_run ?? "never"}</> : null}
      </p>
      {error ? <ErrorBox message={error} /> : null}
      {!root && !error ? <Loading what="agent tree" /> : null}
      {root && focus ? (
        <>
          <nav aria-label="Breadcrumb" className="crumbs cx-crumbs">
            <ol>
              {chain.map((n, i) => (
                <li key={n.id}>
                  {i < chain.length - 1
                    ? <button type="button" className="link" onClick={() => setPath(chain.slice(1, i + 1).map((x) => x.id))}>{n.name}</button>
                    : <span aria-current="location">{n.name}</span>}
                </li>
              ))}
            </ol>
          </nav>
          <div className="focus cx-focus" data-testid={`node-${focus.id}-focus`}>
            <span className="muted">{tierLabel(focus.tier)}</span>
            <StatusBadge id={focus.id} status={focus.status} />
            {focus.current_task ? <span> task: {focus.current_task}</span> : null}
            <span> · cost today <span data-testid={`cost-${focus.id}`}>{focus.cost_today.toFixed(2)}</span></span>
            {rerunUi(focus)}
          </div>
          {focus.children.length === 0 ? (
            <Empty>No child agents below this node.</Empty>
          ) : (
            <ul className="cards cx-grid">
              {focus.children.slice(0, limit).map((c) => (
                <li key={c.id} data-testid={`node-${c.id}`} className={`card cx-tile cx-s-${c.status}`}>
                  {c.children.length > 0 ? (
                    <button type="button" className="link" aria-label={`Open ${c.name}`} onClick={() => setPath([...chain.slice(1).map((x) => x.id), c.id])}>
                      <span>{c.name}</span>
                    </button>
                  ) : <span>{c.name}</span>}{" "}
                  <span className="muted">{tierLabel(c.tier)}</span>{" "}
                  <StatusBadge id={c.id} status={c.status} />
                  {c.current_task ? <div>task: {c.current_task}</div> : null}
                  <div>cost today: <span data-testid={`cost-${c.id}`}>{c.cost_today.toFixed(2)}</span>
                    {" "}· last run: {c.last_run ?? "never"}</div>
                  {rerunUi(c)}
                </li>
              ))}
            </ul>
          )}
          {focus.children.length > limit ? (
            <p><button type="button" className="btn" onClick={() => setLimit((l) => l + CHILD_PAGE)}>
              Show more ({focus.children.length - limit} hidden)</button></p>
          ) : null}
        </>
      ) : null}
      {feed.length > 0 ? (
        <aside className="cx-feed" aria-label="Live activity">
          <h2>Live activity</h2>
          <ol>
            {feed.map((e) => (
              <li key={e.id} className={`cx-ev cx-ev-${e.kind}`}><span className="cx-ev-id">#{e.id}</span> agent {e.agent_id} {e.kind}</li>
            ))}
          </ol>
        </aside>
      ) : null}
    </section>
  );
}
