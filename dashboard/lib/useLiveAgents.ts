"use client";
import { useEffect, useState } from "react";

import { apiGet, buildUrl } from "./api";
import { applyEvent } from "./tree";
import type { AgentEvent, TreeNode, TreeResponse } from "./types";

export interface LiveAgents {
  root: TreeNode | null;
  feed: AgentEvent[];
  live: "connecting" | "live" | "reconnecting" | "unavailable";
  error: string | null;
}

function parse(raw: unknown): AgentEvent | null {
  try {
    const d = JSON.parse(String((raw as MessageEvent).data));
    return typeof d?.id === "number" && typeof d?.agent_id === "number" && typeof d?.kind === "string" ? (d as AgentEvent) : null;
  } catch {
    return null;
  }
}

/** Agent tree kept current from /stream. Same contract as the console: load the tree first, open the
 *  stream at its last event id, apply only newer events, and reload every `resyncMs`. */
export function useLiveAgents(onEvent?: (ev: AgentEvent) => void, resyncMs = 30_000, retryMs = 2_000): LiveAgents {
  const [root, setRoot] = useState<TreeNode | null>(null);
  const [feed, setFeed] = useState<AgentEvent[]>([]);
  const [live, setLive] = useState<LiveAgents["live"]>("connecting");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let disposed = false;
    let gen = 0;
    let es: EventSource | null = null;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const applied = new Set<number>();
    const close = () => { if (es) { es.close(); es = null; } };

    async function sync() {
      const mine = ++gen;
      clearTimeout(timer);
      close();
      let t: TreeResponse;
      try {
        t = await apiGet<TreeResponse>("/agents/tree");
      } catch (e) {
        if (disposed || mine !== gen) return;
        setError(e instanceof Error ? e.message : String(e));
        setLive("reconnecting");
        timer = setTimeout(sync, retryMs * 5);
        return;
      }
      if (disposed || mine !== gen) return;
      setError(null);
      setRoot(t.tree);
      applied.clear();
      timer = setTimeout(sync, resyncMs);
      if (typeof EventSource === "undefined") { setLive("unavailable"); return; }
      const floor = t.last_event_id;
      const source = new EventSource(buildUrl("/stream", { last_event_id: String(floor) }));
      es = source;
      source.onopen = () => { if (es === source && !disposed) setLive("live"); };
      source.addEventListener("agent_event", (raw) => {
        if (es !== source || disposed) return;
        const ev = parse(raw);
        if (!ev || ev.id <= floor || applied.has(ev.id)) return;
        applied.add(ev.id);
        setFeed((f) => [ev, ...f].slice(0, 8));
        setRoot((prev) => (prev ? applyEvent(prev, ev) : prev));
        onEvent?.(ev);
      });
      source.onerror = () => {
        if (es !== source || disposed) return;
        close();
        setLive("reconnecting");
        clearTimeout(timer);
        timer = setTimeout(sync, retryMs);
      };
    }
    void sync();
    return () => { disposed = true; gen++; clearTimeout(timer); close(); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resyncMs, retryMs]);

  return { root, feed, live, error };
}
