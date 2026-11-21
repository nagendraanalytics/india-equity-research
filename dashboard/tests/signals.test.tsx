import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { SignalsView } from "../components/SignalsView";
import { SIGNALS } from "./fixtures";
import { LABEL, installFetch } from "./helpers";

describe("signals view", () => {
  it("signals view shows label, confidence and freshness", async () => {
    installFetch({ "GET /signals": SIGNALS });
    render(<SignalsView />);
    const rows = await screen.findAllByTestId("signal-row");
    expect(rows).toHaveLength(3);
    // label: once prominently and again on every row
    expect(screen.getByTestId("signals-label")).toHaveTextContent(LABEL);
    for (const r of rows) expect(within(r).getByText(LABEL)).toBeInTheDocument();
    expect(screen.getAllByText(LABEL)).toHaveLength(4);

    const alpha = rows[0];
    expect(within(alpha).getByText("Alpha Ltd")).toBeInTheDocument();
    expect(within(alpha).getByTestId("rating")).toHaveTextContent("accumulate");
    expect(within(alpha).getByTestId("confidence")).toHaveTextContent("high");
    expect(within(alpha).getByTestId("freshness")).toHaveTextContent(/as of 2026-11-06/i);
    expect(within(alpha).getByTestId("freshness")).toHaveTextContent(/0 days old/);
    expect(within(alpha).queryByText(/stale/i)).not.toBeInTheDocument();
    expect(within(alpha).getByText("rules-v1")).toBeInTheDocument();
    expect(within(alpha).getByText("growth sub-score above threshold")).toBeInTheDocument();

    const beta = rows[1];
    expect(within(beta).getByTestId("rating")).toHaveTextContent("hold");
    expect(within(beta).getByTestId("confidence")).toHaveTextContent("medium");
    expect(within(beta).getByTestId("freshness")).toHaveTextContent(/36 days old/);
    expect(within(beta).getByText(/stale/i)).toBeInTheDocument();

    expect(within(rows[2]).getByTestId("rating")).toHaveTextContent("insufficient evidence");
    // placeholder threshold note, from the API
    expect(screen.getByTestId("threshold-note")).toHaveTextContent(/7 days/);
    expect(screen.getByTestId("threshold-note")).toHaveTextContent(/placeholder/i);
  });

  it("filters by sector and rating through the API", async () => {
    const { log } = installFetch({ "GET /signals": (url: URL) => {
      const sector = url.searchParams.get("sector");
      const rating = url.searchParams.get("rating");
      const items = SIGNALS.items.filter((i) => (!sector || i.sector === sector) && (!rating || i.rating === rating));
      return { ...SIGNALS, items, count: items.length };
    } });
    const user = userEvent.setup();
    render(<SignalsView />);
    await screen.findAllByTestId("signal-row");
    await within(screen.getByLabelText(/sector/i)).findByRole("option", { name: "Hardware" });
    await user.selectOptions(screen.getByLabelText(/sector/i), "Hardware");
    expect(await screen.findAllByTestId("signal-row")).toHaveLength(1);
    expect(log.at(-1)).toBe("GET /signals?sector=Hardware");
    // sector options survive filtering
    expect(within(screen.getByLabelText(/sector/i)).getByRole("option", { name: "Software" })).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText(/sector/i), "");
    await user.selectOptions(screen.getByLabelText(/rating/i), "hold");
    expect(await screen.findAllByTestId("signal-row")).toHaveLength(1);
    expect(log.at(-1)).toBe("GET /signals?rating=hold");
    const options = within(screen.getByLabelText(/rating/i)).getAllByRole("option").map((o) => o.textContent);
    expect(options).toEqual(["all ratings", "accumulate", "hold", "reduce", "avoid", "insufficient evidence"]);
  });

  it("shows no-data and error states", async () => {
    installFetch({ "GET /signals": { ...SIGNALS, items: [], count: 0 } });
    const { unmount } = render(<SignalsView />);
    expect(await screen.findByText(/no signals/i)).toBeInTheDocument();
    unmount();
    installFetch({ "GET /signals": { status: 500, body: { detail: "database down" } } });
    render(<SignalsView />);
    expect(await screen.findByRole("alert")).toHaveTextContent(/could not load.*database down/i);
  });
});
