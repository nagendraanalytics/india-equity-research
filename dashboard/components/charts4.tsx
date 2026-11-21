const BAD = [234, 95, 85], NEU = [38, 52, 66], OK = [52, 196, 140];
const mix = (a: number[], b: number[], t: number) => `rgb(${a.map((x, i) => Math.round(x + (b[i] - x) * t)).join(",")})`;

/** Diverging colour for a 0–100 score: red below 50, slate at 50, green above. The number is always printed beside it. */
export function scoreColor(s: number | null): string {
  if (s === null) return "var(--panel2)";
  const t = Math.min(100, Math.max(0, s));
  return t < 50 ? mix(BAD, NEU, t / 50) : mix(NEU, OK, (t - 50) / 50);
}

export function Kpi({ label, value, sub, tone }: { label: string; value: string; sub?: string; tone?: "ok" | "bad" | "run" }) {
  return (
    <div className={`tile kpi${tone ? ` tile-${tone}` : ""}`}>
      <span className="muted">{label}</span>
      <b>{value}</b>
      {sub ? <span className="note">{sub}</span> : null}
    </div>
  );
}

/** One dot per company on a 0–100 axis, with a median tick. Dots are jittered by rank so overlaps stay readable. */
export function Strip({ name, dots }: { name: string; dots: { label: string; score: number }[] }) {
  if (dots.length === 0) return <p className="empty">No scores yet.</p>;
  const W = 360, P = 14, x = (v: number) => P + (Math.min(100, Math.max(0, v)) / 100) * (W - 2 * P);
  const sorted = [...dots].sort((a, b) => a.score - b.score);
  const m = sorted[sorted.length >> 1].score;
  return (
    <figure className="chart">
      <svg viewBox={`0 0 ${W} 74`} role="img" aria-label={`${name} score distribution`} style={{ maxWidth: W * 1.5 }}>
        <defs>
          <linearGradient id="stripbar" x1="0" x2="1"><stop offset="0" stopColor={scoreColor(0)} /><stop offset="0.5" stopColor={scoreColor(50)} /><stop offset="1" stopColor={scoreColor(100)} /></linearGradient>
        </defs>
        <rect x={P} y={50} width={W - 2 * P} height={6} rx={3} fill="url(#stripbar)" opacity={0.55} />
        {[0, 50, 100].map((t) => <text key={t} x={x(t)} y={70} fontSize="9" textAnchor="middle" className="c-muted">{t}</text>)}
        <line x1={x(m)} x2={x(m)} y1={8} y2={58} stroke="var(--accent)" strokeDasharray="3 3" />
        {sorted.map((d, i) => (
          <circle key={d.label + i} cx={x(d.score)} cy={42 - ((i % 4) * 9)} r={5} fill={scoreColor(d.score)} stroke="var(--fg)" strokeOpacity={0.55} style={{ filter: "drop-shadow(0 0 4px var(--accent))" }}>
            <title>{`${d.label}: ${d.score.toFixed(1)}`}</title>
          </circle>
        ))}
      </svg>
    </figure>
  );
}

/** Row × column grid of 0–100 scores on the diverging scale. Missing cells show a dash. */
export function ScoreMatrix({ title, rows, cols, cell, rowHref }: {
  title: string; rows: string[]; cols: string[]; cell: (r: string, c: string) => number | null; rowHref?: (r: string) => string | undefined;
}) {
  return (
    <div className="matrix-wrap">
      <table className="matrix" aria-label={title}>
        <thead><tr><th />{cols.map((c) => <th key={c} className="mh">{c}</th>)}</tr></thead>
        <tbody>
          {rows.map((r) => {
            const href = rowHref?.(r);
            return (
              <tr key={r}>
                <td>{href ? <a href={href}>{r}</a> : r}</td>
                {cols.map((c) => { const v = cell(r, c); return <td key={c} className="mc" style={{ background: scoreColor(v) }}>{v === null ? "—" : v.toFixed(0)}</td>; })}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
