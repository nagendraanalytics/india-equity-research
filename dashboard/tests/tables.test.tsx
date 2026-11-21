import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { SectorsView } from "../components/SectorsView";
import { StockView } from "../components/StockView";
import { StocksView } from "../components/StocksView";
import { SECTORS, STOCK, STOCKS } from "./fixtures";
import { LABEL, installFetch } from "./helpers";

describe("sector table", () => {
  it("lists sectors with scores, confidence, freshness and a link to the stock list", async () => {
    installFetch({ "GET /sectors": SECTORS });
    render(<SectorsView />);
    const rows = await screen.findAllByTestId("sector-row");
    expect(rows).toHaveLength(2);
    expect(within(rows[0]).getByRole("link", { name: "Software" })).toHaveAttribute("href", "/stocks?sector=Software");
    expect(within(rows[0]).getByText("65.3")).toBeInTheDocument();
    expect(within(rows[0]).getByText("medium")).toBeInTheDocument();
    expect(within(rows[0]).getByTestId("freshness")).toHaveTextContent(/0 days old/);
    expect(within(rows[1]).getByTestId("freshness")).toHaveTextContent(/no data/i);
    expect(within(rows[1]).getByText(/stale/i)).toBeInTheDocument();
    expect(screen.getByTestId("threshold-note")).toHaveTextContent(/placeholder/i);
  });
  it("shows error state", async () => {
    installFetch({ "GET /sectors": { status: 500, body: { detail: "boom" } } });
    render(<SectorsView />);
    expect(await screen.findByRole("alert")).toHaveTextContent(/boom/);
  });
});

describe("stock list", () => {
  it("is sortable through the sort param and filtered by sector", async () => {
    const { log } = installFetch({ "GET /stocks": STOCKS });
    const user = userEvent.setup();
    render(<StocksView sector="Software" />);
    await screen.findAllByTestId("stock-row");
    expect(log.at(-1)).toBe("GET /stocks?sector=Software&sort=name");
    await user.click(screen.getByRole("button", { name: /sort by long-term score/i }));
    await screen.findAllByTestId("stock-row");
    expect(log.at(-1)).toBe("GET /stocks?sector=Software&sort=long_term_score");
    await user.click(screen.getByRole("button", { name: /sort by long-term score/i }));
    await screen.findAllByTestId("stock-row");
    expect(log.at(-1)).toBe("GET /stocks?sector=Software&sort=-long_term_score");
    const rows = screen.getAllByTestId("stock-row");
    expect(within(rows[0]).getByRole("link", { name: "Alpha Ltd" })).toHaveAttribute("href", "/stocks/INE000A01011");
    expect(within(rows[0]).getByTestId("freshness")).toHaveTextContent(/as of 2026-11-06/i);
    expect(within(rows[1]).getByTestId("freshness")).toHaveTextContent(/no data/i);
    expect(within(rows[1]).getAllByText(/no rating/i).length).toBeGreaterThan(0);
  });
});

describe("stock page", () => {
  it("shows scores, rating label, freshness and red flags; links to the analyst view", async () => {
    installFetch({ "GET /stocks/INE000A01011": STOCK });
    render(<StockView isin="INE000A01011" />);
    expect(await screen.findByRole("heading", { name: /Alpha Ltd/ })).toBeInTheDocument();
    expect(screen.getByText("Auditor resigned in FY25")).toBeInTheDocument();
    expect(screen.getAllByText(LABEL).length).toBeGreaterThan(0);
    expect(screen.getByTestId("freshness")).toHaveTextContent(/as of 2026-11-06/i);
    expect(screen.getByTestId("rating")).toHaveTextContent("accumulate");
    expect(screen.getByRole("link", { name: /analyst view/i })).toHaveAttribute("href", "/analyst/5");
  });
  it("shows 404 as a visible message", async () => {
    installFetch({ "GET /stocks/NOPE": { status: 404, body: { detail: "unknown ISIN 'NOPE'" } } });
    render(<StockView isin="NOPE" />);
    expect(await screen.findByRole("alert")).toHaveTextContent(/unknown ISIN/);
  });
});
