import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { OverviewView } from "../components/OverviewView";
import { RatingBar, Scatter, Sparkline } from "../components/charts";
import { SECTORS, STOCKS } from "./fixtures";
import { installFetch } from "./helpers";

const MORE = {
  ...STOCKS, count: 3,
  items: [...STOCKS.items,
    { ...STOCKS.items[0], isin: "INE000C01013", name: "Gamma Ltd", sector: "Hardware", industry: "Devices",
      long_term_score: 28, short_term_score: 40, rating: "avoid", confidence: "high" }],
};

describe("overview", () => {
  it("shows tiles, charts and one row per stock, best long-term score first", async () => {
    installFetch({ "GET /stocks": MORE, "GET /sectors": SECTORS });
    render(<OverviewView />);
    await screen.findByText("Highest long-term scores");
    expect(screen.getAllByRole("img").length).toBeGreaterThanOrEqual(4);
    const rows = within(screen.getByRole("table")).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(3);
    expect(rows[0].textContent).toContain("Alpha Ltd");
    expect(rows[2].textContent).toContain("Beta Ltd");   // unscored sorts last
  });

  it("filters by search, sector and rating", async () => {
    installFetch({ "GET /stocks": MORE, "GET /sectors": SECTORS });
    render(<OverviewView />);
    await screen.findByText("Highest long-term scores");
    fireEvent.change(screen.getByLabelText("Rating"), { target: { value: "avoid" } });
    expect(within(screen.getByRole("table")).getAllByRole("row")).toHaveLength(2);
    fireEvent.change(screen.getByLabelText("Rating"), { target: { value: "" } });
    fireEvent.change(screen.getByLabelText("Sector"), { target: { value: "Software" } });
    expect(within(screen.getByRole("table")).getAllByRole("row")).toHaveLength(3);
    fireEvent.change(screen.getByLabelText("Search"), { target: { value: "zzz" } });
    expect(screen.getByText("No stocks match these filters.")).toBeTruthy();
  });

  it("keeps the research-signal label and uncalibrated caveat visible", async () => {
    installFetch({ "GET /stocks": STOCKS, "GET /sectors": SECTORS });
    render(<OverviewView />);
    await screen.findByText("Highest long-term scores");
    expect(screen.getByText(/not an order or investment advice/)).toBeTruthy();
    expect(screen.getByText(/not been\s+calibrated/)).toBeTruthy();
  });

  it("shows an error and an empty state", async () => {
    installFetch({ "GET /stocks": { status: 500, body: { detail: "db down" } }, "GET /sectors": SECTORS });
    render(<OverviewView />);
    expect(await screen.findByRole("alert")).toBeTruthy();
  });
});

describe("charts", () => {
  it("rating bar describes its counts; empty input says so", () => {
    const { unmount } = render(<RatingBar counts={{ accumulate: 2, avoid: 1 }} />);
    expect(screen.getByRole("img").getAttribute("aria-label")).toContain("2 accumulate");
    unmount();
    render(<RatingBar counts={{}} />);
    expect(screen.getByText("No stocks to chart.")).toBeTruthy();
  });
  it("scatter and sparkline handle empty and gapped data", () => {
    const a = render(<Scatter dots={[]} />);
    expect(screen.getByText(/No stock has both scores/)).toBeTruthy();
    a.unmount();
    const b = render(<Sparkline label="h" points={[{ date: "d1", value: null }, { date: "d2", value: 50 }, { date: "d3", value: 60 }]} />);
    expect(b.container.querySelector("path")?.getAttribute("d")).toMatch(/^M[\d. ]+L/);
    b.unmount();
    render(<Sparkline label="h" points={[{ date: "d1", value: null }]} />);
    expect(screen.getByText("no history")).toBeTruthy();
  });
});
