"use client";
// Second chart set: donut, stacked bars, histogram, heatmap, stacked area, columns, horizontal bars,
// and the fleet grid (one cell per agent, built to stay readable at 2000+ agents).
import type { ReactNode } from "react";

export interface Slice { label: string; value: number; color: string }

const arc = (cx: number, cy: number, r: number, a0: number, a1: number) => {
  const p = (a: number) => [cx + r * Math.cos(a), cy + r * Math.sin(a)];
  const [x0, y0] = p(a0), [x1, y1] = p(a1);
  return `M${x0.toFixed(2)} ${y0.toFixed(2)} A${r} ${r} 0 ${a1 - a0 > Math.PI ? 1 : 0} 1 ${x1.toFixed(2)} ${y1.toFixed(2)}`;
};

/** Donut with the total in the middle and a legend. */
export function Donut({ slices, title, centre }: { slices: Slice[]; title: string; centre?: string }) {
  const parts = slices.filter((s) => s.value > 0);
  const total = parts.reduce((a, s) => a + s.value, 0);
  if (total === 0) return <p className="empty">Nothing to chart.</p>;
  let a = -Math.PI / 2;
  return (
    <figure className="chart">
      <svg viewBox="0 0 160 160" role="img" aria-label={`${title}: ${parts.map((s) => `${s.value} ${s.label}`).join(", ")}`} style={{ maxWidth: 220, margin: "0 auto" }}>
        {parts.map((s) => {
          const sweep = (s.value / total) * Math.PI * 2;
          const end = a + Math.min(sweep, Math.PI * 2 - 0.0001);
          const d = arc(80, 80, 58, a, end);
          a += sweep;
          return <path key={s.label} d={d} fill="none" stroke={s.color} strokeWidth="20"><title>{`${s.label}: ${s.value}`}</title></path>;
        })}
        <text x="80" y="82" textAnchor="middle" fontSize="24" fontWeight="600" className="c-ink">{centre ?? total}</text>
        <text x="80" y="100" textAnchor="middle" fontSize="10" className="c-muted">total</text>
      </svg>
      <figcaption className="legend">{parts.map((s) => <span key={s.label}><i style={{ background: s.color }} />{s.label} {s.value}</span>)}</figcaption>
    </figure>
  );
}

export interface StackRow { label: string; parts: Record<string, number> }

/** One horizontal stacked bar per row (e.g. status mix per tier). */
export function StackedBars({ rows, colors, title }: { rows: StackRow[]; colors: Record<string, string>; title: string }) {
  const W = 360, labelW = 90, h = 24;
  const max = Math.max(1, ...rows.map((r) => Object.values(r.parts).reduce((a, b) => a + b, 0)));
  if (rows.length === 0) return <p className="empty">Nothing to chart.</p>;
  const keys = Object.keys(colors);
  return (
    <figure className="chart">
      <svg viewBox={`0 0 ${W} ${rows.length * h + 4}`} role="img" aria-label={title} style={{ maxWidth: W * 1.5 }}>
        {rows.map((r, i) => {
          let x = labelW;
          return (
            <g key={r.label} transform={`translate(0 ${i * h})`}>
              <text x={labelW - 6} y={15} textAnchor="end" fontSize="11" className="c-ink">{r.label}</text>
              {keys.map((k) => {
                const v = r.parts[k] ?? 0;
                if (!v) return null;
                const w = (v / max) * (W - labelW - 36);
                const el = <rect key={k} x={x} y={3} width={w} height={16} fill={colors[k]}><title>{`${r.label} ${k}: ${v}`}</title></rect>;
                x += w;
                return el;
              })}
              <text x={x + 4} y={15} fontSize="10" className="c-muted">{Object.values(r.parts).reduce((a, b) => a + b, 0)}</text>
            </g>
          );
        })}
      </svg>
      <figcaption className="legend">{keys.map((k) => <span key={k}><i style={{ background: colors[k] }} />{k}</span>)}</figcaption>
    </figure>
  );
}

/** Histogram of 0–100 scores in equal bins. */
export function Histogram({ values, bins = 10, title }: { values: number[]; bins?: number; title: string }) {
  if (values.length === 0) return <p className="empty">No scores to chart.</p>;
  const W = 360, H = 130, P = 22;
  const counts = Array.from({ length: bins }, () => 0);
  for (const v of values) counts[Math.min(bins - 1, Math.max(0, Math.floor((v / 100) * bins)))]++;
  const max = Math.max(...counts);
  const bw = (W - P - 6) / bins;
  return (
    <figure className="chart">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={title} style={{ maxWidth: W * 1.5 }}>
        {counts.map((c, i) => {
          const h = (c / max) * (H - P - 14);
          return (
            <g key={i}>
              <rect x={P + i * bw + 1} y={H - P - h} width={bw - 2} height={h} rx={2} fill="var(--accent)" fillOpacity={0.8}>
                <title>{`${(i * 100) / bins}–${((i + 1) * 100) / bins}: ${c}`}</title></rect>
              {c > 0 ? <text x={P + i * bw + bw / 2} y={H - P - h - 3} textAnchor="middle" fontSize="9" className="c-muted">{c}</text> : null}
            </g>
          );
        })}
        <line x1={P} x2={W - 6} y1={H - P} y2={H - P} className="c-grid" />
        {[0, 50, 100].map((t) => <text key={t} x={P + (t / 100) * (W - P - 6)} y={H - 6} textAnchor="middle" fontSize="9" className="c-dim">{t}</text>)}
      </svg>
    </figure>
  );
}

export interface HeatCell { value: number; color: string }

/** Row × column grid of counts; opacity scales with the count. */
export function Heatmap({ rows, cols, cell, title }: {
  rows: string[]; cols: string[]; cell: (r: string, c: string) => HeatCell; title: string;
}) {
  if (rows.length === 0) return <p className="empty">Nothing to chart.</p>;
  const labelW = 110, cw = 56, ch = 26, top = 34;
  const W = labelW + cols.length * cw, H = top + rows.length * ch + 4;
  const all = rows.flatMap((r) => cols.map((c) => cell(r, c).value));
  const max = Math.max(1, ...all);
  return (
    <figure className="chart">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={title} style={{ maxWidth: W * 1.6 }}>
        {cols.map((c, j) => (
          <text key={c} x={labelW + j * cw + cw / 2} y={22} textAnchor="middle" fontSize="9" className="c-muted">{c.length > 11 ? c.slice(0, 10) + "…" : c}</text>
        ))}
        {rows.map((r, i) => (
          <g key={r}>
            <text x={labelW - 6} y={top + i * ch + 17} textAnchor="end" fontSize="11" className="c-ink">{r.length > 16 ? r.slice(0, 15) + "…" : r}</text>
            {cols.map((c, j) => {
              const v = cell(r, c);
              return (
                <g key={c}>
                  <rect x={labelW + j * cw + 2} y={top + i * ch + 2} width={cw - 4} height={ch - 4} rx={4} className="c-plot" />
                  {v.value > 0 ? <rect x={labelW + j * cw + 2} y={top + i * ch + 2} width={cw - 4} height={ch - 4} rx={4} fill={v.color}
                        fillOpacity={0.25 + 0.75 * (v.value / max)}><title>{`${r} · ${c}: ${v.value}`}</title></rect> : null}
                  {v.value > 0 ? <text x={labelW + j * cw + cw / 2} y={top + i * ch + 17} textAnchor="middle" fontSize="11" className="c-ink">{v.value}</text> : null}
                </g>
              );
            })}
          </g>
        ))}
      </svg>
    </figure>
  );
}

export interface AreaSeries { key: string; color: string; values: number[] }

/** Stacked area over an evenly spaced x axis (e.g. events per minute). */
export function StackedArea({ series, labels, title }: { series: AreaSeries[]; labels: string[]; title: string }) {
  const n = labels.length;
  const W = 360, H = 150, P = 26;
  const totals = Array.from({ length: n }, (_, i) => series.reduce((a, s) => a + (s.values[i] ?? 0), 0));
  const max = Math.max(1, ...totals);
  if (n < 2 || totals.every((t) => t === 0)) return <p className="empty">No activity in this window yet.</p>;
  const x = (i: number) => P + (i / (n - 1)) * (W - P - 6);
  const y = (v: number) => H - 20 - (v / max) * (H - 34);
  const base = Array.from({ length: n }, () => 0);
  const layers = series.map((s) => {
    const lo = [...base];
    const hi = s.values.map((v, i) => { base[i] += v; return base[i]; });
    const top = hi.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)} ${y(v).toFixed(1)}`).join(" ");
    const bottom = lo.map((v, i) => `L${x(n - 1 - i).toFixed(1)} ${y(lo[n - 1 - i]).toFixed(1)}`).join(" ");
    return { s, d: `${top} ${bottom} Z` };
  });
  return (
    <figure className="chart">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={title} style={{ maxWidth: W * 1.5 }}>
        {[0, 0.5, 1].map((f) => <g key={f}><line x1={P} x2={W - 6} y1={y(max * f)} y2={y(max * f)} className="c-grid" />
          <text x={P - 4} y={y(max * f) + 3} textAnchor="end" fontSize="9" className="c-dim">{Math.round(max * f)}</text></g>)}
        {layers.map(({ s, d }) => <path key={s.key} d={d} fill={s.color} fillOpacity={0.7} stroke={s.color} />)}
        <text x={P} y={H - 5} fontSize="9" className="c-dim">{labels[0]}</text>
        <text x={W - 6} y={H - 5} fontSize="9" textAnchor="end" className="c-dim">{labels[n - 1]}</text>
      </svg>
      <figcaption className="legend">{series.map((s) => <span key={s.key}><i style={{ background: s.color }} />{s.key}</span>)}</figcaption>
    </figure>
  );
}

/** Vertical columns, one per label. */
export function Columns({ labels, values, title, color = "var(--accent)", unit = "" }: {
  labels: string[]; values: number[]; title: string; color?: string; unit?: string;
}) {
  const n = values.length;
  const W = 360, H = 130, P = 30;
  const max = Math.max(1, ...values);
  if (n === 0 || values.every((v) => v === 0)) return <p className="empty">No data in this window yet.</p>;
  const bw = (W - P - 6) / n;
  return (
    <figure className="chart">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={title} style={{ maxWidth: W * 1.5 }}>
        <line x1={P} x2={W - 6} y1={H - 20} y2={H - 20} className="c-grid" />
        <text x={P - 4} y={16} textAnchor="end" fontSize="9" className="c-dim">{max >= 1000 ? `${Math.round(max / 1000)}k` : max}</text>
        {values.map((v, i) => {
          const h = (v / max) * (H - 38);
          return <rect key={i} x={P + i * bw + 1} y={H - 20 - h} width={Math.max(1, bw - 2)} height={h} rx={2} fill={color}><title>{`${labels[i]}: ${v}${unit}`}</title></rect>;
        })}
        <text x={P} y={H - 5} fontSize="9" className="c-dim">{labels[0]}</text>
        <text x={W - 6} y={H - 5} fontSize="9" textAnchor="end" className="c-dim">{labels[n - 1]}</text>
      </svg>
    </figure>
  );
}

export interface HRow { label: string; value: number; color?: string; note?: ReactNode }

/** Horizontal bars on a free scale (counts, cost). */
export function HBars({ rows, title, color = "var(--accent)" }: { rows: HRow[]; title: string; color?: string }) {
  if (rows.length === 0) return <p className="empty">Nothing to chart.</p>;
  const W = 360, labelW = 130, h = 22;
  const max = Math.max(1e-9, ...rows.map((r) => r.value));
  return (
    <figure className="chart">
      <svg viewBox={`0 0 ${W} ${rows.length * h + 4}`} role="img" aria-label={title} style={{ maxWidth: W * 1.5 }}>
        {rows.map((r, i) => {
          const w = (r.value / max) * (W - labelW - 50);
          return (
            <g key={r.label + i} transform={`translate(0 ${i * h})`}>
              <text x={labelW - 6} y={15} textAnchor="end" fontSize="11" className="c-ink">{r.label.length > 20 ? r.label.slice(0, 19) + "…" : r.label}</text>
              <rect x={labelW} y={4} width={Math.max(1, w)} height={13} rx={3} fill={r.color ?? color}><title>{`${r.label}: ${r.value}`}</title></rect>
              <text x={labelW + w + 4} y={15} fontSize="10" className="c-muted">{Number.isInteger(r.value) ? r.value : r.value.toFixed(2)}</text>
            </g>
          );
        })}
      </svg>
    </figure>
  );
}

export interface FleetCellData { id: number; name: string; tier: number; status: string }

/** One small square per agent, packed in rows. Colour = status. Scales to several thousand cells. */
export function FleetGrid({ cells, colors, selectedId, onPick }: {
  cells: FleetCellData[]; colors: Record<string, string>; selectedId: number | null; onPick: (id: number) => void;
}) {
  if (cells.length === 0) return <p className="empty">No agents match these filters.</p>;
  const size = 11, gap = 2, W = 960;
  const perRow = Math.floor(W / (size + gap));
  const rows = Math.ceil(cells.length / perRow);
  return (
    <figure className="chart">
      <svg viewBox={`0 0 ${W} ${rows * (size + gap)}`} role="group" aria-label={`Fleet map: ${cells.length} agents`}>
        {cells.map((c, i) => (
          <rect key={c.id} className={`fleet-cell${c.id === selectedId ? " sel" : ""}`} x={(i % perRow) * (size + gap)} y={Math.floor(i / perRow) * (size + gap)}
                width={size} height={size} rx={2} fill={colors[c.status] ?? colors.idle} onClick={() => onPick(c.id)}>
            <title>{`${c.name} — ${c.status}`}</title>
          </rect>
        ))}
      </svg>
    </figure>
  );
}
