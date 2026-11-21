import { vi } from "vitest";

export const BASE = "http://127.0.0.1:8000";
export const LABEL = "research signal — not an order or investment advice";

export type Handler = (url: URL, init?: RequestInit) => { status?: number; body: unknown } | unknown;

/** Install a fake fetch routed by "METHOD /path". Every call is appended to `log` as
 *  "METHOD /path?query" so tests can assert on call order and parameters. */
export function installFetch(routes: Record<string, Handler | unknown>, log: string[] = []) {
  const fn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input));
    const method = (init?.method ?? "GET").toUpperCase();
    log.push(`${method} ${url.pathname}${url.search}`);
    const route = routes[`${method} ${url.pathname}`];
    if (route === undefined) {
      return new Response(JSON.stringify({ detail: "no such route" }), { status: 404 });
    }
    const out: any = typeof route === "function" ? await (route as Handler)(url, init) : route;
    const isEnvelope = out && typeof out === "object" && "body" in out && ("status" in out);
    const status = isEnvelope ? out.status ?? 200 : 200;
    const body = isEnvelope ? out.body : out;
    return new Response(JSON.stringify(body), {
      status,
      headers: { "content-type": "application/json" },
    });
  });
  vi.stubGlobal("fetch", fn);
  return { fn, log };
}

/** Minimal EventSource double. Instances register themselves so tests can drive them. */
export class FakeEventSource {
  static instances: FakeEventSource[] = [];
  static onCreate: ((es: FakeEventSource) => void) | null = null;
  url: string;
  closed = false;
  onerror: ((e?: unknown) => void) | null = null;
  onopen: ((e?: unknown) => void) | null = null;
  onmessage: ((e: unknown) => void) | null = null;
  listeners: Record<string, Array<(e: unknown) => void>> = {};
  constructor(url: string | URL) {
    this.url = String(url);
    FakeEventSource.instances.push(this);
    FakeEventSource.onCreate?.(this);
  }
  addEventListener(type: string, fn: (e: unknown) => void) {
    (this.listeners[type] ||= []).push(fn);
  }
  removeEventListener(type: string, fn: (e: unknown) => void) {
    this.listeners[type] = (this.listeners[type] || []).filter((f) => f !== fn);
  }
  close() {
    this.closed = true;
  }
  /** Deliver an agent_event frame (what the API sends). */
  emit(ev: { id: number; agent_id: number; kind: string; payload?: unknown; cost?: number }) {
    if (this.closed) return;
    const e = { data: JSON.stringify({ payload: {}, cost: 0, created_at: null, ...ev }), lastEventId: String(ev.id) };
    (this.listeners["agent_event"] || []).forEach((f) => f(e));
    this.onmessage?.(e);
  }
  /** Deliver even though close() was called: models a frame already in flight on a dead connection. */
  deliverEvenIfClosed(ev: { id: number; agent_id: number; kind: string; payload?: unknown }) {
    const e = { data: JSON.stringify({ payload: {}, cost: 0, created_at: null, ...ev }), lastEventId: String(ev.id) };
    (this.listeners["agent_event"] || []).forEach((f) => f(e));
  }
  fail() {
    this.onerror?.({ type: "error" });
  }
  static reset() {
    FakeEventSource.instances = [];
    FakeEventSource.onCreate = null;
  }
}

export function installEventSource() {
  FakeEventSource.reset();
  vi.stubGlobal("EventSource", FakeEventSource);
  return FakeEventSource;
}

const WORDS = /\b(buy|buys|buying|sell|sells|selling|order|orders|ordering)\b/i;
/** Text with the mandated research-signal label removed, then checked for trading wording. */
export function tradingWords(text: string): string[] {
  const cleaned = text.split(LABEL).join("");
  const m = cleaned.match(new RegExp(WORDS.source, "gi"));
  return m ?? [];
}
