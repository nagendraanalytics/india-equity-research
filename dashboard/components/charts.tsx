"use client";
import { ratingColor, RATINGS, BANDS } from "../lib/ratings";
import { bandStance } from "../lib/signal";

const W = 360;

/** Horizontal stacked bar of counts per rating. */
export function RatingBar({ counts }: { counts: Record<string, number> }) {
  const keys = [...RATINGS, "no rating"].filter((k) => (counts[k] ?? 0) > 0);
  const total = keys.reduce((a, k) => a + counts[k], 0);
  if (total === 0) return <p className="empty">No stocks to chart.</p>;
  let x = 0;
  return (
    <figure className="chart">
      <svg viewBox={`0 0 ${W} 34`} role="img" aria-label={`Rating distribution: ${keys.map((k) => `${counts[k]} ${k}`).join(", ")}`}>
        {keys.map((k) => {
          const w = (counts[k] / total) * W;
          const r = <rect key={k} x={x} y={0} width={w} height={26} fill={ratingColor(k)}><title>{`${k}: ${counts[k]}`}</title></rect>;
          x += w;
          return r;
        })}
      </svg>
      <figcaption className="legend">
        {keys.map((k) => (<span key={k}><i style={{ background: ratingColor(k) }} />{k} {counts[k]}</span>))}
      </figcaption>
    </figure>
  );
}

export interface BarRow { label: string; value: number | null; color?: string; href?: string; note?: string }

/** Horizontal bars on a 0–100 scale with the placeholder band guides. */
export function ScoreBars({ rows, title }: { rows: BarRow[]; title: string }) {
  const h = 22;
  const labelW = 130;
  const plot = W - labelW - 40;
  if (rows.length === 0) return <p className="empty">Nothing to chart.</p>;
  return (
    <figure className="chart">
      <svg viewBox={`0 0 ${W} ${rows.length * h + 14}`} role="img" aria-label={title}>
        {Object.values(BANDS).map((b) => (
          <line key={b} x1={labelW + (b / 100) * plot} x2={labelW + (b / 100) * plot} y1={0} y2={rows.length * h}
                className="c-grid" strokeDasharray="3 3" />
        ))}
        {rows.map((r, i) => (
          <g key={r.label + i} transform={`translate(0 ${i * h})`}>
            <text x={labelW - 6} y={14} textAnchor="end" fontSize="11" className="c-ink">
              {r.label.length > 22 ? r.label.slice(0, 21) + "…" : r.label}
            </text>
            {r.value === null ? (
              <text x={labelW + 2} y={14} fontSize="11" className="c-muted">no score</text>
            ) : (
              <>
                <rect x={labelW} y={4} width={Math.max(1, (Math.min(100, Math.max(0, r.value)) / 100) * plot)} height={13}
                      fill={r.color ?? "var(--accent)"}><title>{`${r.label}: ${r.value.toFixed(1)}`}</title></rect>
                <text x={labelW + (Math.min(100, r.value) / 100) * plot + 4} y={14} fontSize="11" className="c-muted">{r.value.toFixed(1)}</text>
              </>
            )}
          </g>
        ))}
        <text x={labelW} y={rows.length * h + 11} fontSize="9" className="c-dim">0</text>
        <text x={labelW + plot} y={rows.length * h + 11} fontSize="9" className="c-dim" textAnchor="end">100</text>
      </svg>
    </figure>
  );
}

export interface Dot { label: string; x: number; y: number; rating: string | null; href?: string }

/** Long-term (x) vs short-term (y) scatter, one dot per scored stock. */
export function Scatter({ dots }: { dots: Dot[] }) {
  const S = 280, P = 30;
  const pos = (v: number) => P + (Math.min(100, Math.max(0, v)) / 100) * (S - P - 8);
  if (dots.length === 0) return <p className="empty">No stock has both scores yet.</p>;
  return (
    <figure className="chart">
      <svg viewBox={`0 0 ${S} ${S}`} role="img" aria-label={`Long-term versus short-term score for ${dots.length} stocks`}>
        <rect x={P} y={8} width={S - P - 8} height={S - P - 8} className="c-plot" />
        {[50].map((v) => (<g key={v}><line x1={pos(v)} x2={pos(v)} y1={8} y2={S - P} className="c-grid" /><line y1={S - P - (v / 100) * (S - P - 8)} y2={S - P - (v / 100) * (S - P - 8)} x1={P} x2={S - 8} className="c-grid" /></g>))}
        {dots.map((d) => (
          <circle key={d.label} cx={pos(d.x)} cy={S - P - (Math.min(100, Math.max(0, d.y)) / 100) * (S - P - 8)} r={6}
                  fill={ratingColor(d.rating)} fillOpacity={0.85} className="c-halo"><title>{`${d.label}: long ${d.x.toFixed(1)}, short ${d.y.toFixed(1)}`}</title></circle>
        ))}
        <text x={S / 2} y={S - 6} textAnchor="middle" fontSize="10" className="c-muted">long-term score →</text>
        <text transform={`translate(10 ${S / 2}) rotate(-90)`} textAnchor="middle" fontSize="10" className="c-muted">short-term score →</text>
      </svg>
    </figure>
  );
}

export interface Point { date: string; value: number | null }

/** Small line over time; gaps (null) break the line. */
export function Sparkline({ points, label, w = 160, h = 40 }: { points: Point[]; label: string; w?: number; h?: number }) {
  const vals = points.filter((p) => p.value !== null);
  if (vals.length === 0) return <span className="muted">no history</span>;
  const x = (i: number) => (points.length === 1 ? w / 2 : 4 + (i / (points.length - 1)) * (w - 8));
  const y = (v: number) => h - 4 - (Math.min(100, Math.max(0, v)) / 100) * (h - 8);
  let d = "";
  let pen = false;
  points.forEach((p, i) => {
    if (p.value === null) { pen = false; return; }
    d += `${pen ? "L" : "M"}${x(i).toFixed(1)} ${y(p.value).toFixed(1)} `;
    pen = true;
  });
  return (
    <svg viewBox={`0 0 ${w} ${h}`} width={w} height={h} role="img" aria-label={label}>
      <path d={d} className="c-line" strokeWidth="1.5" />
      {points.map((p, i) => p.value === null ? null : <circle key={i} cx={x(i)} cy={y(p.value)} r={2.5} className="c-dotfill"><title>{`${p.date}: ${p.value.toFixed(1)}`}</title></circle>)}
    </svg>
  );
}

/** Inline score cell: number plus a thin bar. */
export function ScoreCell({ v }: { v: number | null }) {
  if (v === null) return <span className="muted">—</span>;
  return (
    <span className="scorecell">
      <span className="track"><span className="fill" style={{ width: `${Math.min(100, Math.max(0, v))}%`, background: ratingColor(bandStance(v)) }} /></span>
      {v.toFixed(1)}
    </span>
  );
}
