import type { ChartPanel, ChartSpec } from "../lib/types";

const W = 720, L = 46, R = 12;
const HEIGHT: Record<ChartPanel["kind"], number> = { price: 170, line: 120, bars: 90 };
const COLOURS = ["var(--accent)", "var(--warn)", "#b4aaff", "var(--hold)"];
const DASH = "6 4";

const compact = (v: number) => (Math.abs(v) >= 1000 ? v.toFixed(0) : Math.abs(v) >= 10 ? v.toFixed(1) : v.toFixed(2));

function pathOf(vals: Array<number | null>, x: (i: number) => number, y: (v: number) => number): string {
  let d = "", pen = false;
  vals.forEach((v, i) => {
    if (v === null || !Number.isFinite(v)) { pen = false; return; }
    d += `${pen ? "L" : "M"}${x(i).toFixed(1)} ${y(v).toFixed(1)} `;
    pen = true;
  });
  return d.trim();
}

/** Stacked panels (price/line/bars) sharing one x axis. Pure SVG, no chart library. */
export function PriceChart({ spec }: { spec: ChartSpec }) {
  if (spec.panels.length === 0 || spec.dates.length === 0) {
    return <p className="empty">Needs more history: {spec.caption}</p>;
  }
  const n = spec.dates.length;
  const x = (i: number) => (n === 1 ? (L + W - R) / 2 : L + (i / (n - 1)) * (W - L - R));
  let top = 6;
  const blocks = spec.panels.map((p, pi) => {
    const h = HEIGHT[p.kind];
    const y0 = top + 16;
    top = y0 + h + 14;
    const nums = p.series.flatMap((s) => s.values).filter((v): v is number => v !== null && Number.isFinite(v));
    const bandNums = p.y_bands.flatMap((b) => [b.lo, b.hi]);
    let lo = Math.min(...nums, ...(p.kind === "bars" ? [0] : bandNums), ...(nums.length ? [] : [0]));
    let hi = Math.max(...nums, ...(p.kind === "bars" ? [] : bandNums), ...(nums.length ? [] : [1]));
    if (!(hi > lo)) { const pad = Math.abs(lo) * 0.01 || 1; lo -= pad; hi += pad; }
    const y = (v: number) => y0 + h - ((Math.min(hi, Math.max(lo, v)) - lo) / (hi - lo)) * h;
    return { p, pi, y0, h, y, lo, hi };
  });
  const H = top + 16;
  const markerX = spec.markers.map((m) => ({ ...m, i: spec.dates.indexOf(m.day) })).filter((m) => m.i >= 0);
  const lineSeries = spec.panels.flatMap((p) => p.series.filter((s) => s.style === "line" || s.style === "dashed"));
  return (
    <figure className="chart pricechart">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={spec.caption}>
        {blocks.map(({ p, pi, y0, h, y, lo, hi }) => {
          const hiS = p.series.find((s) => s.style === "band_hi");
          const loS = p.series.find((s) => s.style === "band_lo");
          let area = "";
          if (hiS && loS) {
            const idx = hiS.values.map((_, i) => i).filter((i) => hiS.values[i] !== null && loS.values[i] !== null);
            if (idx.length > 1) {
              area = idx.map((i, k) => `${k ? "L" : "M"}${x(i).toFixed(1)} ${y(hiS.values[i] as number).toFixed(1)}`).join(" ")
                + " " + [...idx].reverse().map((i) => `L${x(i).toFixed(1)} ${y(loS.values[i] as number).toFixed(1)}`).join(" ") + " Z";
            }
          }
          const colourIdx = (name: string) => Math.max(0, lineSeries.findIndex((s) => s.name === name)) % COLOURS.length;
          const bw = Math.max(1, ((W - L - R) / n) * 0.7);
          return (
            <g key={pi} data-panel={p.title}>
              <text x={L} y={y0 - 5} fontSize="10" className="c-muted">{p.title}</text>
              <rect x={L} y={y0} width={W - L - R} height={h} className="c-plot" />
              {p.y_bands.map((b, k) => (
                <g key={k}>
                  <rect x={L} y={Math.min(y(b.lo), y(b.hi))} width={W - L - R} height={Math.abs(y(b.lo) - y(b.hi))} fill="var(--warn)" fillOpacity={0.12} />
                  <text x={W - R - 4} y={Math.min(y(b.lo), y(b.hi)) + 10} textAnchor="end" fontSize="9" className="c-dim">{b.label}</text>
                </g>
              ))}
              {area ? <path d={area} fill="var(--accent)" fillOpacity={0.1} aria-label={`${hiS!.name} to ${loS!.name}`} /> : null}
              {p.series.map((s) => {
                if (s.style === "bar") {
                  return (
                    <g key={s.name} aria-label={s.name}>
                      {s.values.map((v, i) => v === null || !Number.isFinite(v) ? null : (
                        <rect key={i} data-bar x={x(i) - bw / 2} y={Math.min(y(v), y(0))} width={bw} height={Math.max(0.5, Math.abs(y(v) - y(Math.max(lo, 0))))} fill="var(--accent)" fillOpacity={0.6} />
                      ))}
                    </g>
                  );
                }
                if (s.style === "band_hi" || s.style === "band_lo") return null;
                const ci = colourIdx(s.name);
                return <path key={s.name} d={pathOf(s.values, x, y)} fill="none" stroke={COLOURS[ci]} strokeWidth={s.style === "line" ? 1.8 : 1.2}
                             {...(s.style === "dashed" ? { strokeDasharray: DASH } : {})} aria-label={s.name} />;
              })}
              <text x={L - 4} y={y0 + 8} textAnchor="end" fontSize="9" className="c-dim">{compact(hi)}</text>
              <text x={L - 4} y={y0 + h} textAnchor="end" fontSize="9" className="c-dim">{compact(lo)}</text>
            </g>
          );
        })}
        {markerX.map((m, k) => (
          <g key={k} aria-label={`marker ${k + 1}`}>
            <line x1={x(m.i)} x2={x(m.i)} y1={blocks[0].y0} y2={H - 16} stroke="var(--dim)" strokeDasharray="2 3" />
            <circle cx={x(m.i)} cy={blocks[0].y0 + 6} r={5} fill="var(--bg)" stroke="var(--fg)" />
            <text x={x(m.i)} y={blocks[0].y0 + 9} textAnchor="middle" fontSize="8" className="c-ink">{k + 1}</text>
          </g>
        ))}
        <text x={L} y={H - 4} fontSize="9" className="c-dim">{spec.dates[0]}</text>
        <text x={W - R} y={H - 4} textAnchor="end" fontSize="9" className="c-dim">{spec.dates[n - 1]}</text>
      </svg>
      <figcaption>
        {spec.caption}
        {spec.as_of ? <span className="muted"> As of {spec.as_of}.</span> : null}
      </figcaption>
      <ul className="legend" aria-label="Chart legend">
        {lineSeries.map((s, i) => (
          <li key={s.name}>
            <svg width="26" height="8" aria-hidden="true"><line x1="0" x2="26" y1="4" y2="4" stroke={COLOURS[i % COLOURS.length]} strokeWidth="2" {...(s.style === "dashed" ? { strokeDasharray: DASH } : {})} /></svg>{" "}
            <span>{s.name}</span>
          </li>
        ))}
      </ul>
      {markerX.length > 0 ? (
        <ol className="chart-markers" aria-label="Marked days">
          {markerX.map((m, k) => <li key={k}>{m.day}: {m.label}</li>)}
        </ol>
      ) : null}
    </figure>
  );
}
