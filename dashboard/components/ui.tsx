import type { ReactNode } from "react";

export function Loading({ what }: { what: string }) {
  return <p className="note">Loading {what}…</p>;
}
export function ErrorBox({ message }: { message: string }) {
  return <p role="alert" className="error">Could not load: {message}</p>;
}
export function Empty({ children }: { children: ReactNode }) {
  return <p className="empty">{children}</p>;
}
export const fmt = (n: number | null | undefined, digits = 1) =>
  n === null || n === undefined ? "—" : Number(n).toFixed(digits);

/** Render arbitrary JSON from the API as plain text nodes (never markup). */
export function Value({ v }: { v: unknown }): ReactNode {
  if (v === null || v === undefined) return <span className="muted">none</span>;
  if (Array.isArray(v)) {
    if (v.length === 0) return <span className="muted">none recorded</span>;
    return <ul>{v.map((x, i) => <li key={i}><Value v={x} /></li>)}</ul>;
  }
  if (typeof v === "object") {
    const e = Object.entries(v as Record<string, unknown>);
    if (e.length === 0) return <span className="muted">none recorded</span>;
    return (
      <dl className="kv">
        {e.map(([k, x]) => (<div key={k}><dt>{k}</dt><dd><Value v={x} /></dd></div>))}
      </dl>
    );
  }
  return <>{String(v)}</>;
}

export function RatingBadge({ rating }: { rating: string | null }) {
  if (!rating) return <span data-testid="rating" className="badge badge-none">no rating</span>;
  return <span data-testid="rating" className={`badge rating rating-${rating.replace(/\s+/g, "-")}`}>{rating}</span>;
}
