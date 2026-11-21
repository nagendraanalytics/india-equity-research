import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({ usePathname: () => "/algorithms/stock/INE009A01021" }));

import { AlgoCard } from "../components/AlgoCard";
import { AlgoExplainer } from "../components/AlgoExplainer";
import { AlgosOverview } from "../components/AlgosOverview";
import { Nav } from "../components/Nav";
import { SectorAlgos } from "../components/SectorAlgos";
import { StockAlgos } from "../components/StockAlgos";
import { CATALOGUE, ISIN, ROUTES, SERIES, STOCK } from "./algos_fixtures";
import { installFetch, tradingWords } from "./helpers";

describe("navigation", () => {
  it("has an Algorithms link that is current on algorithm pages", () => {
    render(<Nav />);
    expect(screen.getByRole("link", { name: "Algorithms" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "Stocks" })).not.toHaveAttribute("aria-current");
  });
});

describe("StockAlgos", () => {
  it("lists every family, and fetches a chart only after it is opened", async () => {
    const { log } = installFetch({ ...ROUTES, [`GET /stocks/${ISIN}/algos/mom_3m/series`]: SERIES });
    const { container } = render(<StockAlgos isin={ISIN} />);
    for (const fam of ["momentum", "trend", "value_quality"]) expect(await screen.findByRole("heading", { name: new RegExp(fam.replace("_", "[ _]"), "i") })).toBeInTheDocument();
    expect(log.some((l) => l.includes("/series"))).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: /Show chart: 3-month return/ }));
    await waitFor(() => expect(log.some((l) => l.startsWith(`GET /stocks/${ISIN}/algos/mom_3m/series`))).toBe(true));
    expect(await screen.findByRole("img", { name: "Rolling return caption" })).toBeInTheDocument();
    expect(screen.getByText(/placeholder/i)).toBeInTheDocument();
    expect(tradingWords(container.textContent ?? "")).toEqual([]);
  });

  it("shows needs-data rows without a chart button", async () => {
    installFetch(ROUTES);
    render(<StockAlgos isin={ISIN} />);
    expect(await screen.findByText(/needs trailing four-quarter EPS/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Show chart: Price to earnings/ })).not.toBeInTheDocument();
  });

  it("shows composite dials, agreement and coverage", async () => {
    installFetch(ROUTES);
    render(<StockAlgos isin={ISIN} />);
    expect(await screen.findByRole("img", { name: /Long-term composite: 62\.5 of 100/ })).toBeInTheDocument();
    expect(screen.getByText(/80% of algorithms agree/)).toBeInTheDocument();
    expect(screen.getByText(/3 of 4 algorithms have a score/)).toBeInTheDocument();
  });

  it("shows a visible error when the API fails", async () => {
    installFetch({ [`GET /stocks/${ISIN}/algos`]: { status: 500, body: { detail: "boom" } }, "GET /algos": CATALOGUE });
    render(<StockAlgos isin={ISIN} />);
    expect(await screen.findByRole("alert")).toHaveTextContent(/boom/);
  });

  it("explains a chart that cannot be drawn", async () => {
    installFetch({ ...ROUTES, [`GET /stocks/${ISIN}/algos/rsi14/series`]: { ...SERIES, panels: [], dates: [], caption: "needs 15 daily prices" } });
    render(<StockAlgos isin={ISIN} />);
    fireEvent.click(await screen.findByRole("button", { name: /Show chart: RSI/ }));
    expect(await screen.findByText(/needs more history/i)).toHaveTextContent(/needs 15 daily prices/);
  });
});

describe("AlgoCard", () => {
  it("states rules score, score with algorithms, and that the weight is off", async () => {
    installFetch(ROUTES);
    render(<AlgoCard isin={ISIN} />);
    expect(await screen.findByText(/Rules score 61\.0/)).toBeInTheDocument();
    expect(screen.getByText(/with algorithms 61\.0/)).toBeInTheDocument();
    expect(screen.getByText(/weight 0%, off/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /See all algorithms/ })).toHaveAttribute("href", `/algorithms/stock/${ISIN}`);
  });

  it("says so, softly, when the API has nothing", async () => {
    installFetch({});
    render(<AlgoCard isin={ISIN} />);
    expect(await screen.findByText(/Algorithm scores unavailable/)).toBeInTheDocument();
  });

  it("names a positive and a negative lean in words", async () => {
    installFetch(ROUTES);
    render(<AlgoCard isin={ISIN} />);
    expect(await screen.findByText(/positive lean/)).toBeInTheDocument();
    expect(screen.getByText(/negative lean/)).toBeInTheDocument();
  });
});

describe("SectorAlgos / AlgosOverview / AlgoExplainer", () => {
  it("sector page ranks companies and links to their algorithm pages", async () => {
    installFetch(ROUTES);
    const { container } = render(<SectorAlgos macro="Information Technology" />);
    const rows = await screen.findAllByTestId("algo-sector-row");
    expect(rows).toHaveLength(2);
    expect(within(rows[0]).getByRole("link", { name: "Alpha Ltd" })).toHaveAttribute("href", `/algorithms/stock/${ISIN}`);
    expect(tradingWords(container.textContent ?? "")).toEqual([]);
  });

  it("overview shows weight state, sector heatmap and most positive / negative leans", async () => {
    installFetch(ROUTES);
    const { container } = render(<AlgosOverview />);
    expect(await screen.findByText(/Algorithm weight in final scores: 0% \(off\)/)).toBeInTheDocument();
    expect(await screen.findByRole("img", { name: /Average algorithm score by sector and family/ })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /Most positive/ })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /Most negative/ })).toBeInTheDocument();
    expect(tradingWords(container.textContent ?? "")).toEqual([]);
  });

  it("explainer shows the definition, the placeholder band and the universe table", async () => {
    installFetch(ROUTES);
    const { container } = render(<AlgoExplainer algoId="mom_3m" />);
    expect(await screen.findByRole("heading", { name: /3-month return/ })).toBeInTheDocument();
    expect(screen.getByText(/Scores 0 at -20 and 100 at 20/)).toBeInTheDocument();
    expect(screen.getAllByTestId("algo-universe-row")).toHaveLength(2);
    expect(tradingWords(container.textContent ?? "")).toEqual([]);
  });

  it("explainer reports an unknown algorithm", async () => {
    installFetch({ "GET /algos/nope/universe": { status: 404, body: { detail: "unknown algorithm 'nope'" } } });
    render(<AlgoExplainer algoId="nope" />);
    expect(await screen.findByRole("alert")).toHaveTextContent(/unknown algorithm/);
  });
});
