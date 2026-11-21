const fmtN = (v: number) => (Number.isInteger(v) ? String(v) : v.toFixed(1));

/** Radar of 0–100 values, one axis per label; a text list repeats every value. */
export function Radar({ labels, values, title }: { labels: string[]; values: Array<number | null>; title: string }) {
  const S = 260, c = S / 2, r = 90, n = labels.length;
  const pt = (i: number, v: number) => {
    const a = -Math.PI / 2 + (2 * Math.PI * i) / n;
    return [c + Math.cos(a) * r * (v / 100), c + Math.sin(a) * r * (v / 100)] as const;
  };
  const poly = values.map((v, i) => pt(i, Math.min(100, Math.max(0, v ?? 0)))).map((p) => p.map((q) => q.toFixed(1)).join(",")).join(" ");
  const summary = labels.map((l, i) => `${l} ${values[i] === null ? "no score" : fmtN(values[i] as number)}`).join(", ");
  return (
    <figure className="chart">
      <svg viewBox={`0 0 ${S} ${S}`} role="img" aria-label={`${title}: ${summary}`}>
        {[25, 50, 75, 100].map((ring) => (
          <polygon key={ring} points={labels.map((_, i) => pt(i, ring).map((q) => q.toFixed(1)).join(",")).join(" ")} fill="none" className="c-grid" />
        ))}
        {labels.map((l, i) => {
          const [x, y] = pt(i, 112);
          return <text key={l} x={x} y={y} fontSize="10" textAnchor="middle" className="c-muted">{l}</text>;
        })}
        <polygon data-radar points={poly} fill="var(--accent)" fillOpacity={0.25} stroke="var(--accent)" strokeWidth={1.8} />
      </svg>
      <ul className="legend">
        {labels.map((l, i) => <li key={l}>{l}: {values[i] === null ? "no score" : fmtN(values[i] as number)}</li>)}
      </ul>
    </figure>
  );
}

/** Ring dial for one 0–100 score. */
export function Dial({ value, title }: { value: number | null; title: string }) {
  const r = 38, C = 2 * Math.PI * r;
  const v = value === null ? 0 : Math.min(100, Math.max(0, value));
  return (
    <figure className="chart dial">
      <svg viewBox="0 0 100 100" width="110" height="110" role="img" aria-label={value === null ? `${title}: no score` : `${title}: ${value.toFixed(1)} of 100`}>
        <circle cx="50" cy="50" r={r} fill="none" className="c-grid" strokeWidth="9" />
        {value === null ? null : <circle cx="50" cy="50" r={r} fill="none" stroke="var(--accent)" strokeWidth="9" strokeDasharray={`${(v / 100) * C} ${C}`} transform="rotate(-90 50 50)" strokeLinecap="round" />}
        <text x="50" y="55" textAnchor="middle" fontSize="18" className="c-ink">{value === null ? "—" : value.toFixed(0)}</text>
      </svg>
      <figcaption>{title}</figcaption>
    </figure>
  );
}

export interface Zone { from: number; to: number; label: string }

/** Half-circle gauge with labelled zones and a needle. */
export function Gauge({ value, zones, title, min, max }: { value: number; zones: Zone[]; title: string; min: number; max: number }) {
  const cx = 100, cy = 95, r = 75;
  const ang = (v: number) => Math.PI * (1 - (Math.min(max, Math.max(min, v)) - min) / (max - min));
  const p = (a: number, rad = r) => `${(cx + Math.cos(a) * rad).toFixed(1)} ${(cy - Math.sin(a) * rad).toFixed(1)}`;
  const zone = zones.find((z, i) => value >= z.from && (value < z.to || i === zones.length - 1));
  const FILL = ["var(--bad)", "var(--warn)", "var(--ok)", "var(--hold)"];
  return (
    <figure className="chart">
      <svg viewBox="0 0 200 110" role="img" aria-label={`${title}: ${value.toFixed(1)}${zone ? ` (${zone.label})` : ""}`}>
        {zones.map((z, i) => (
          <path key={z.label} d={`M${p(ang(z.from))} A${r} ${r} 0 0 1 ${p(ang(z.to))}`} fill="none" stroke={FILL[i % FILL.length]} strokeWidth="14" strokeOpacity={0.7} />
        ))}
        <line x1={cx} y1={cy} x2={p(ang(value), r - 10).split(" ")[0]} y2={p(ang(value), r - 10).split(" ")[1]} stroke="var(--fg)" strokeWidth="2.5" />
        <circle cx={cx} cy={cy} r="4" fill="var(--fg)" />
        <text x={cx} y={cy + 12} textAnchor="middle" fontSize="11" className="c-ink">{value.toFixed(1)}</text>
      </svg>
      <ul className="legend">{zones.map((z) => <li key={z.label}>{z.label}: {z.from} to {z.to}</li>)}</ul>
    </figure>
  );
}

/** Pass / fail / no data tick boxes, each with a word as well as a mark. */
export function TickBoxes({ labels, checked, title }: { labels: string[]; checked: Array<boolean | null>; title: string }) {
  return (
    <figure className="chart">
      <figcaption>{title}</figcaption>
      <ul className="tickboxes">
        {labels.map((l, i) => (
          <li key={l}>
            <span aria-hidden="true">{checked[i] === true ? "✓" : checked[i] === false ? "✗" : "–"}</span> {l} — <strong>{checked[i] === true ? "pass" : checked[i] === false ? "fail" : "no data"}</strong>
          </li>
        ))}
      </ul>
    </figure>
  );
}
