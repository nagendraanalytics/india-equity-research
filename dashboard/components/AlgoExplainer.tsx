"use client";
import Link from "next/link";

import { bandText, familyLabel, horizonLabel } from "../lib/algos";
import { useApi } from "../lib/useApi";
import type { AlgoUniverse } from "../lib/types";
import { ScoreCell } from "./charts";
import { Histogram } from "./charts2";
import { Empty, ErrorBox, Loading, fmt } from "./ui";

export function AlgoExplainer({ algoId }: { algoId: string }) {
  const res = useApi<AlgoUniverse>(`/algos/${encodeURIComponent(algoId)}/universe`);
  if (res.state === "loading") return <Loading what="algorithm" />;
  if (res.state === "error") return <ErrorBox message={res.message} />;
  const { algo, items } = res.data;
  const scores = items.map((i) => i.score).filter((s): s is number => s !== null);
  return (
    <section>
      <h1>{algo.name}</h1>
      <p className="muted">{familyLabel(algo.family)} · {horizonLabel(algo.horizon)} · needs {algo.min_history} daily prices</p>
      <p>{algo.description}</p>
      <p className="note">{bandText(algo.band)}</p>
      <p><Link href="/algorithms">All algorithms</Link></p>
      <h2>Scores across companies</h2>
      {items.length === 0 ? <Empty>No company has a score for this algorithm yet.</Empty> : (
        <>
          <Histogram values={scores} title={`Distribution of ${algo.name} scores`} />
          <table>
            <thead><tr><th>Company</th><th>Score</th><th>Value</th><th>Why</th><th>As of</th></tr></thead>
            <tbody>
              {items.map((i) => (
                <tr key={i.isin} data-testid="algo-universe-row">
                  <td><Link href={`/algorithms/stock/${encodeURIComponent(i.isin)}`}>{i.name}</Link></td>
                  <td><ScoreCell v={i.score} /></td>
                  <td>{fmt(i.value, 2)}</td>
                  <td className="muted">{i.reason}</td>
                  <td className="muted">{i.as_of}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </section>
  );
}
