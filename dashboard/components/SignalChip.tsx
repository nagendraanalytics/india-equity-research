import { signalOf } from "../lib/signal";
import type { StockItem } from "../lib/types";

/** Colour-coded research stance. A solid chip is a rating; a dashed chip is only a lean taken from the
 *  score (placeholder band edges, not calibrated) because the evidence is too thin to rate. */
export function SignalChip({ s }: { s: StockItem }) {
  const g = signalOf(s);
  if (!g.stance) return <span className="chip chip-none" data-testid="signal">no score</span>;
  const why = g.provisional
    ? `Not rated: ${s.rating ?? "no rating"}${s.confidence ? ` (confidence ${s.confidence})` : ""}. Lean taken from the ${g.basis}-term score of ${g.score?.toFixed(1)} using the uncalibrated placeholder bands.`
    : `Rated ${g.stance}${s.confidence ? `, confidence ${s.confidence}` : ""}.`;
  return (
    <span className={`chip chip-${g.stance}${g.provisional ? " chip-lean" : ""}`} data-testid="signal" title={why}>
      {g.text}
      {g.provisional ? <small>provisional</small> : null}
      <span className="sr-only" data-testid="rating">{s.rating ?? "no rating"}</span>
    </span>
  );
}
