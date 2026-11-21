import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { PriceChart } from "../components/PriceChart";
import { Dial, Gauge, Radar, TickBoxes } from "../components/charts3";
import type { ChartSpec } from "../lib/types";
import { getCatalogue, getSeries, getStockAlgos } from "../lib/algos";
import { installFetch } from "./helpers";

const dates = ["2026-11-17", "2026-11-18", "2026-11-19", "2026-11-20"];
const base: ChartSpec = {
  dates, caption: "Close with the 50-day and 200-day averages", as_of: "2026-11-20", markers: [],
  panels: [{ title: "Price", kind: "price", y_bands: [],
    series: [{ name: "Close", values: [1, 2, 3, 4], style: "line" }, { name: "50-day average", values: [null, 1.5, 2, 2.5], style: "dashed" }] }],
};

describe("PriceChart", () => {
  it("is an image named by its caption, with one group per panel and a named line per series", () => {
    const { container } = render(<PriceChart spec={{ ...base, panels: [base.panels[0], { title: "RSI", kind: "line", y_bands: [{ lo: 70, hi: 100, label: "overbought zone" }], series: [{ name: "RSI", values: [40, 50, 60, 75], style: "line" }] }] }} />);
    expect(screen.getByRole("img", { name: base.caption })).toBeInTheDocument();
    expect(container.querySelectorAll("g[data-panel]").length).toBe(2);
    expect(container.querySelector('[aria-label="Close"]')).not.toBeNull();
    expect(container.querySelector('[aria-label="50-day average"]')).toHaveAttribute("stroke-dasharray");
    expect(screen.getByText(/overbought zone/)).toBeInTheDocument();
    expect(screen.getByText(/as of 2026-11-20/i)).toBeInTheDocument();
  });

  it("says it needs more history when there are no panels", () => {
    render(<PriceChart spec={{ dates: [], panels: [], markers: [], caption: "needs 200 daily prices", as_of: null }} />);
    expect(screen.getByText(/needs more history/i)).toHaveTextContent(/needs 200 daily prices/);
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
  });

  it("shows marker labels as text, not only as dots", () => {
    render(<PriceChart spec={{ ...base, markers: [{ day: "2026-11-19", label: "50-day crossed above 200-day" }] }} />);
    expect(screen.getByText(/50-day crossed above 200-day/)).toBeInTheDocument();
  });

  it("draws bars for a bars panel and survives null gaps", () => {
    const { container } = render(<PriceChart spec={{ ...base, panels: [{ title: "Volume", kind: "bars", y_bands: [], series: [{ name: "Volume", values: [5, null, 7, 9], style: "bar" }] }] }} />);
    expect(container.querySelectorAll('rect[data-bar]').length).toBe(3);
  });

  it("does not crash on a flat series", () => {
    const { container } = render(<PriceChart spec={{ ...base, panels: [{ title: "Flat", kind: "line", y_bands: [], series: [{ name: "Flat", values: [5, 5, 5, 5], style: "line" }] }] }} />);
    expect(container.querySelector("path")?.getAttribute("d")).not.toMatch(/NaN/);
  });
});

describe("charts3", () => {
  it("Radar has one polygon and a text list of the values", () => {
    const { container } = render(<Radar title="Family averages" labels={["trend", "momentum", "risk"]} values={[62, null, 40]} />);
    expect(screen.getByRole("img", { name: /Family averages/ })).toBeInTheDocument();
    expect(container.querySelectorAll("polygon[data-radar]").length).toBe(1);
    const list = screen.getByRole("list");
    expect(within(list).getByText(/trend: 62/)).toBeInTheDocument();
    expect(within(list).getByText(/momentum: no score/)).toBeInTheDocument();
  });

  it("Dial shows the number and a text alternative; null shows a dash", () => {
    render(<Dial title="Long-term" value={72.4} />);
    expect(screen.getByRole("img", { name: /Long-term: 72\.4 of 100/ })).toBeInTheDocument();
    render(<Dial title="Short-term" value={null} />);
    expect(screen.getByRole("img", { name: /Short-term: no score/ })).toBeInTheDocument();
  });

  it("Gauge names the zone the value falls in", () => {
    render(<Gauge title="Distress score" value={2.1} min={0} max={4} zones={[{ from: 0, to: 1.8, label: "distress zone" }, { from: 1.8, to: 3, label: "grey zone" }, { from: 3, to: 4, label: "safe zone" }]} />);
    expect(screen.getByRole("img", { name: /Distress score: 2\.1.*grey zone/ })).toBeInTheDocument();
  });

  it("TickBoxes mark pass and fail with text, not colour alone", () => {
    render(<TickBoxes title="Piotroski checks" labels={["Profit up", "Cash flow positive", "Leverage down"]} checked={[true, false, null]} />);
    expect(screen.getByText(/Profit up/)).toHaveTextContent(/pass/);
    expect(screen.getByText(/Cash flow positive/)).toHaveTextContent(/fail/);
    expect(screen.getByText(/Leverage down/)).toHaveTextContent(/no data/);
  });
});

describe("algos api helpers", () => {
  it("call the right routes", async () => {
    const { log } = installFetch({
      "GET /algos": { items: [], count: 0, blend_weight: 0, max_blend_weight: 0.3, notes: [] },
      "GET /stocks/INE1/algos": { isin: "INE1", items: [] },
      "GET /stocks/INE1/algos/rsi14/series": base,
    });
    await getCatalogue();
    await getStockAlgos("INE1");
    await getSeries("INE1", "rsi14", 120);
    expect(log).toEqual(["GET /algos", "GET /stocks/INE1/algos", "GET /stocks/INE1/algos/rsi14/series?points=120"]);
  });
});
