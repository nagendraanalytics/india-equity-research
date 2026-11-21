import type { Fresh } from "../lib/types";

/** As-of date, age and stale flag, all straight from the API. */
export function Freshness({ as_of, age_days, stale }: Fresh) {
  return (
    <span data-testid="freshness" className="freshness">
      {as_of ? (
        <>As of {as_of}{age_days !== null ? <> · {age_days} days old</> : null}</>
      ) : (
        <>No data (as-of date unknown)</>
      )}
      {stale ? <span className="badge badge-stale">stale</span> : null}
    </span>
  );
}

export function ThresholdNote({ days, status }: { days: number; status: string }) {
  return (
    <p data-testid="threshold-note" className="note">
      Data counts as stale after {days} days.{" "}
      {status === "PLACEHOLDER" ? "This threshold is a placeholder and has not been reviewed." : null}
    </p>
  );
}
