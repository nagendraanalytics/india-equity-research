export const API_BASE = (process.env.NEXT_PUBLIC_API_BASE ?? "http://127.0.0.1:8000").replace(/\/+$/, "");

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

function detailText(body: unknown, fallback: string): string {
  const d = (body as { detail?: unknown } | null)?.detail;
  if (typeof d === "string") return d;
  if (Array.isArray(d)) return d.map((x) => (x && typeof x === "object" && "msg" in x ? String((x as any).msg) : String(x))).join("; ");
  return fallback;
}

export function buildUrl(path: string, params?: Record<string, string | undefined | null>): string {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params ?? {})) if (v) q.set(k, v);
  const s = q.toString();
  return `${API_BASE}${path}${s ? `?${s}` : ""}`;
}

async function request<T>(method: "GET" | "POST", path: string, params?: Record<string, string | undefined | null>): Promise<T> {
  let res: Response;
  try {
    res = await fetch(buildUrl(path, params), { method, cache: "no-store" });
  } catch (e) {
    throw new ApiError(0, `cannot reach the API at ${API_BASE} (${e instanceof Error ? e.message : "network error"})`);
  }
  let body: unknown = null;
  try { body = await res.json(); } catch { /* non-JSON body */ }
  if (!res.ok) throw new ApiError(res.status, detailText(body, `HTTP ${res.status}`));
  return body as T;
}

export const apiGet = <T,>(path: string, params?: Record<string, string | undefined | null>) => request<T>("GET", path, params);
export const apiPost = <T,>(path: string) => request<T>("POST", path);
