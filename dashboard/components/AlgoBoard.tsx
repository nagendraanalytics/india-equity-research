import Link from "next/link";

import { familyLabel, lean } from "../lib/algos";
import type { AlgoScoreItem } from "../lib/types";
import { ScoreCell } from "./charts";
import { fmt } from "./ui";

/** Scoreboard grouped by family: score, raw value, a word for the lean, the reason and the as-of date. */
export function AlgoBoard({ items }: { items: AlgoScoreItem[] }) {
  const families = Array.from(new Set(items.map((i) => i.family)));
  return (
    <div>
      {families.map((f) => (
        <section key={f}>
          <p className="table-cap">{familyLabel(f)}</p>
          <table>
            <thead><tr><th>Algorithm</th><th>Score</th><th>Reading</th><th>Value</th><th>Why</th><th>As of</th></tr></thead>
            <tbody>
              {items.filter((i) => i.family === f).map((i) => (
                <tr key={i.algo_id} data-testid="algo-row">
                  <td><Link href={`/algorithms/${encodeURIComponent(i.algo_id)}`}>{i.name}</Link></td>
                  <td><ScoreCell v={i.score} /></td>
                  <td>{lean(i.score)}</td>
                  <td>{fmt(i.value, 2)}</td>
                  <td className="muted">{i.reason}</td>
                  <td className="muted">{i.as_of}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      ))}
    </div>
  );
}
