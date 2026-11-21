import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { LiveFlow } from "../components/LiveFlow";
import { OverviewView } from "../components/OverviewView";
import { bandStance, bySignal, signalOf } from "../lib/signal";
import type { StockItem } from "../lib/types";
import { SECTORS, STOCKS } from "./fixtures";
import { installEventSource, installFetch } from "./helpers";

const base = STOCKS.items[0] as unknown as StockItem;
const mk = (name: string, over: Partial<StockItem>): StockItem => ({ ...base, name, isin: name, nse_code: null, ...over });

describe("signal logic", () => {
  it("band edges are 70 / 50 / 30", () => {
    expect([bandStance(70), bandStance(69.9), bandStance(50), bandStance(30), bandStance(29.9)])
      .toEqual(["accumulate", "hold", "hold", "reduce", "avoid"]);
  });

  it("a rated stock keeps its rating; an unrated one only leans, and says so", () => {
    expect(signalOf(mk("a", { rating: "hold", long_term_score: 90 }))).toMatchObject({ stance: "hold", provisional: false });
    const lean = signalOf(mk("b", { rating: "insufficient evidence", long_term_score: null, short_term_score: 14 }));
    expect(lean).toMatchObject({ stance: "avoid", provisional: true, basis: "short", text: "leans avoid" });
    expect(signalOf(mk("c", { rating: null, long_term_score: null, short_term_score: null }))).toMatchObject({ stance: null, rank: 99 });
  });

  it("sorts rated stances first (best to worst), then leans, then unscored; higher score first within a group", () => {
    const rows = [
      mk("unscored", { rating: null, long_term_score: null, short_term_score: null }),
      mk("lean-avoid", { rating: "insufficient evidence", long_term_score: 10 }),
      mk("avoid", { rating: "avoid", long_term_score: 20 }),
      mk("lean-acc", { rating: "insufficient evidence", long_term_score: 80 }),
      mk("hold-low", { rating: "hold", long_term_score: 51 }),
      mk("hold-high", { rating: "hold", long_term_score: 65 }),
      mk("acc", { rating: "accumulate", long_term_score: 75 }),
    ];
    expect([...rows].sort(bySignal).map((r) => r.name))
      .toEqual(["acc", "hold-high", "hold-low", "avoid", "lean-acc", "lean-avoid", "unscored"]);
  });
});

describe("signal board", () => {
  const ITEMS = {
    ...STOCKS, count: 3,
    items: [
      { ...base, isin: "I1", name: "Low", rating: "insufficient evidence", long_term_score: null, short_term_score: 14, confidence: "low" },
      { ...base, isin: "I2", name: "Mid", rating: "insufficient evidence", long_term_score: null, short_term_score: 60.3, confidence: "low" },
      { ...base, isin: "I3", name: "Top", rating: "accumulate", long_term_score: 80, short_term_score: 70, confidence: "high" },
    ],
  };

  it("opens sorted by signal, marks leans as provisional, and the stance chips filter", async () => {
    installFetch({ "GET /stocks": ITEMS, "GET /sectors": SECTORS });
    render(<OverviewView />);
    await screen.findByText("All stocks");
    const order = () => screen.getAllByTestId("stock-row").map((r) => r.textContent ?? "");
    expect(order().map((t) => ["Top", "Mid", "Low"].find((n) => t.includes(n)))).toEqual(["Top", "Mid", "Low"]);
    const rows = screen.getAllByTestId("stock-row");
    expect(within(rows[0]).getByTestId("signal")).toHaveTextContent("accumulate");
    expect(within(rows[0]).getByTestId("signal").className).not.toContain("chip-lean");
    expect(within(rows[1]).getByTestId("signal")).toHaveTextContent(/leans hold.*provisional/i);
    expect(within(rows[1]).getByTestId("signal").className).toContain("chip-lean");
    fireEvent.click(screen.getByRole("button", { name: /^1\s*avoid/i }));
    expect(screen.getAllByTestId("stock-row")).toHaveLength(1);
    expect(screen.getByTestId("stock-row")).toHaveTextContent("Low");
  });
});

describe("live flow", () => {
  const n = (id: number, tier: number, name: string, status: string, children: unknown[] = []) =>
    ({ id, tier, name, status, current_task: null, last_run: null, cost_today: 0, children });
  const TREE = {
    last_event_id: 10,
    tree: n(1, 1, "master", "idle", [
      n(7, 7, "macro", "idle", [n(70, 8, "fx_usdinr", "completed"), n(71, 8, "fred_macro", "failed")]),
      n(8, 7, "events", "idle", [n(80, 8, "announcements", "idle")]),
      n(2, 2, "Information Technology", "completed", [n(5, 5, "Alpha Ltd", "running")]),
    ]),
  };

  it("draws sources, controllers and companies, counts states, and reacts to a streamed event", async () => {
    const ES = installEventSource();
    installFetch({ "GET /agents/tree": TREE });
    render(<LiveFlow />);
    expect(await screen.findAllByTestId("lf-source")).toHaveLength(3);
    expect(screen.getAllByTestId("lf-segment")).toHaveLength(2);
    expect(screen.getAllByTestId("lf-company")).toHaveLength(1);
    expect(screen.getByTestId("live-counts")).toHaveTextContent("1 running · 0 queued · 1 failed");
    const es = ES.instances[ES.instances.length - 1];
    expect(es.url).toContain("last_event_id=10");
    act(() => es.onopen?.());
    act(() => es.emit({ id: 11, agent_id: 80, kind: "started", payload: {}, cost: 0 }));
    const src = screen.getAllByTestId("lf-source").find((g) => g.textContent?.includes("announcements"))!;
    expect(src.getAttribute("class")).toContain("lf-running");
    expect(src.getAttribute("class")).toContain("hot");
    expect(screen.getByLabelText("Latest agent events")).toHaveTextContent("announcements started");
  });

  it("says why when the API is unreachable instead of drawing nothing", async () => {
    installEventSource();
    installFetch({});
    render(<LiveFlow />);
    expect(await screen.findByText(/Live view unavailable/)).toBeInTheDocument();
  });
});
