import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({ usePathname: () => "/algorithms/family/momentum" }));

import { AlgoTabs } from "../components/AlgoTabs";
import { FamilyDashboard } from "../components/FamilyDashboard";
import { scoreColor } from "../components/charts4";
import { familyInsights, stockInsights } from "../lib/insights";
import { CATALOGUE, FAMILY_ROUTES, ROUTES, STOCK } from "./algos_fixtures";
import { installFetch, tradingWords } from "./helpers";

describe("FamilyDashboard", () => {
  it("shows KPIs, a ranked company-by-algorithm matrix, distributions and insights", async () => {
    installFetch(FAMILY_ROUTES);
    const { container } = render(<FamilyDashboard family="momentum" />);
    expect(await screen.findByRole("heading", { name: "Momentum analysis" })).toBeInTheDocument();
    expect(await screen.findByText("Companies scored")).toBeInTheDocument();
    const rows = await screen.findAllByTestId("matrix-row");
    expect(rows).toHaveLength(2);
    expect(within(rows[0]).getByRole("link", { name: "Alpha Ltd" })).toHaveAttribute("href", "/algorithms/stock/INE009A01021");
    expect(screen.getByRole("table", { name: /Momentum scores by company and algorithm/ })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: /3-month return score distribution/ })).toBeInTheDocument();
    expect(screen.getAllByTestId("insight").length).toBeGreaterThan(0);
    expect(tradingWords(container.textContent ?? "")).toEqual([]);
  });

  it("explains how to get data when no company has a score", async () => {
    installFetch({ ...FAMILY_ROUTES, "GET /algos/mom_3m/universe": { ...FAMILY_ROUTES["GET /algos/mom_3m/universe"], items: [], count: 0 },
                   "GET /algos/rsi14/universe": { ...FAMILY_ROUTES["GET /algos/rsi14/universe"], items: [], count: 0 } });
    render(<FamilyDashboard family="momentum" />);
    expect(await screen.findByText(/No company has momentum scores yet/)).toBeInTheDocument();
    expect(screen.getByText(/price CSV/i)).toBeInTheDocument();
  });

  it("rejects an unknown family", async () => {
    installFetch(ROUTES);
    render(<FamilyDashboard family="nonsense" />);
    expect(await screen.findByRole("alert")).toBeInTheDocument();
  });
});

describe("helpers", () => {
  it("scoreColor is red below 50, green above, neutral at 50, grey for none", () => {
    expect(scoreColor(null)).toMatch(/var\(--/);
    const lo = scoreColor(10), mid = scoreColor(50), hi = scoreColor(90);
    expect(new Set([lo, mid, hi]).size).toBe(3);
  });
  it("stockInsights names the strongest and weakest readings without trading words", () => {
    const out = stockInsights(STOCK.items);
    expect(out.join(" ")).toMatch(/3-month return/);
    expect(tradingWords(out.join(" "))).toEqual([]);
  });
  it("familyInsights reports lean share and disagreement", () => {
    const out = familyInsights("momentum", [{ name: "A", scores: [80, 20, 50] }, { name: "B", scores: [60, 62, 64] }], [{ name: "X", avg: 70 }, { name: "Y", avg: 30 }]);
    expect(out.join(" ")).toMatch(/1 of 2 companies/);
    expect(out.join(" ")).toMatch(/A/);
  });
  it("AlgoTabs lists the families and marks the current one", () => {
    render(<AlgoTabs />);
    expect(screen.getByRole("link", { name: "Momentum" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "Overview" })).not.toHaveAttribute("aria-current");
    expect(CATALOGUE.count).toBe(4);
  });
});
