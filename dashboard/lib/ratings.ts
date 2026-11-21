/** Research stances in display order, with chart colours. Labels are research stances, not orders. */
export const RATINGS = ["accumulate", "hold", "reduce", "avoid", "insufficient evidence"] as const;
export const RATING_COLOR: Record<string, string> = {
  accumulate: "#5fc38f", hold: "#6c9bd1", reduce: "#f0b84e", avoid: "#ff7a6b",
  "insufficient evidence": "#6d8296", "no rating": "#46596a",
};
export const ratingColor = (r: string | null | undefined) => RATING_COLOR[r ?? "no rating"] ?? "#46596a";
/** Band edges from config/rules (placeholders, uncalibrated): shown only as chart guides. */
export const BANDS = { accumulate: 70, hold: 50, reduce: 30 } as const;
