import { render, screen } from "@testing-library/react";
import type { ReactElement } from "react";
import { describe, expect, it } from "vitest";

import { AnalystView } from "../components/AnalystView";
import { ConsoleView } from "../components/ConsoleView";
import { Nav } from "../components/Nav";
import { OverviewView } from "../components/OverviewView";
import { SectorsView } from "../components/SectorsView";
import { SignalsView } from "../components/SignalsView";
import { StockView } from "../components/StockView";
import { StocksView } from "../components/StocksView";
import { safeHttpUrl } from "../lib/safe";
import { ANALYST, HISTORY, SECTORS, SIGNALS, STOCK, STOCKS, SUMMARY, TREE } from "./fixtures";
import { installEventSource, installFetch, tradingWords } from "./helpers";

/** Visible text plus the accessible-name attributes a reader or screen reader would get. */
function allText(container: HTMLElement): string {
  const attrs = [...container.querySelectorAll("[aria-label],[title],[placeholder],[alt]")]
    .flatMap((el) => ["aria-label", "title", "placeholder", "alt"].map((a) => el.getAttribute(a) ?? ""));
  return [container.textContent ?? "", ...attrs].join(" | ");
}

const never = () => new Promise<never>(() => {});
const err = { status: 500, body: { detail: "database down" } };

type Case = [string, () => ReactElement, string | RegExp | null];
const dataViews = (): Case[] => [
  ["console", () => <ConsoleView />, "Healthcare"],
  ["signals", () => <SignalsView />, "Beta Ltd"],
  ["analyst", () => <AnalystView agentId={5} />, "Auditor resigned in FY25"],
  ["overview", () => <OverviewView />, "Highest long-term scores"],
  ["sectors", () => <SectorsView />, "Hardware"],
  ["stocks", () => <StocksView />, "Beta Ltd"],
  ["stock", () => <StockView isin="INE000A01011" />, "Customer concentration"],
];

describe("no trading wording", () => {
  it("loaded views: no rendered text or accessible attribute contains trading words (mandated label excepted)", async () => {
    installEventSource();
    installFetch({
      "GET /agents/tree": TREE, "GET /agents/summary": SUMMARY, "GET /signals": SIGNALS,
      "GET /analysts/5": ANALYST, "GET /sectors": SECTORS, "GET /stocks": STOCKS,
      "GET /stocks/INE000A01011": STOCK, "GET /stocks/INE000A01011/history": HISTORY,
    });
    const nav = render(<Nav />);
    expect(tradingWords(allText(nav.container))).toEqual([]);
    nav.unmount();
    for (const [name, el, wait] of dataViews()) {
      const { container, unmount } = render(el());
      await screen.findAllByText(wait!);
      const text = allText(container);
      expect(text.length, name).toBeGreaterThan(20);
      expect(tradingWords(text), `${name}: ${text}`).toEqual([]);
      unmount();
    }
  });

  it("loading, error and empty states are clean too", async () => {
    installEventSource();
    const states: Array<[string, Record<string, unknown>, (c: HTMLElement) => Promise<void> | void]> = [
      ["loading", Object.fromEntries(["tree", "summary"].map((k) => [`GET /agents/${k}`, never]).concat(
        [["GET /signals", never], ["GET /analysts/5", never], ["GET /sectors", never], ["GET /stocks", never],
         ["GET /stocks/INE000A01011", never]])), () => {}],
      ["error", { "GET /agents/tree": err, "GET /agents/summary": err, "GET /signals": err, "GET /analysts/5": err,
                  "GET /sectors": err, "GET /stocks": err, "GET /stocks/INE000A01011": err }, () => {}],
      ["empty", { "GET /agents/tree": { last_event_id: 0, tree: { ...TREE.tree, children: [] } },
                  "GET /agents/summary": SUMMARY,
                  "GET /signals": { ...SIGNALS, items: [], count: 0 }, "GET /sectors": { ...SECTORS, items: [], count: 0 },
                  "GET /stocks": { ...STOCKS, items: [], count: 0 },
                  "GET /analysts/5": { ...ANALYST, findings: null, evidence: [], run_history: [], confidence: null },
                  "GET /stocks/INE000A01011": { ...STOCK, signal: null, rating: null, sub_scores: null, benefits: [], risks: [], red_flags: [] } },
       () => {}],
    ];
    for (const [state, routes] of states) {
      installFetch(routes);
      for (const [name, el] of dataViews()) {
        const { container, unmount } = render(el());
        if (state !== "loading") {
          await screen.findAllByText(state === "error" ? /could not load|database down/i : /./);
          await new Promise((r) => setTimeout(r, 30));
        }
        const text = allText(container);
        expect(text.length, `${state} ${name}`).toBeGreaterThan(5);
        expect(tradingWords(text), `${state} ${name}: ${text}`).toEqual([]);
        unmount();
      }
    }
  });

  it("the check itself catches inflections and exempts only the exact label", () => {
    expect(tradingWords("Please BUY now")).toEqual(["BUY"]);
    expect(tradingWords("selling, buying, orders, ordering")).toEqual(["selling", "buying", "orders", "ordering"]);
    expect(tradingWords("sort order")).toEqual(["order"]);
    expect(tradingWords("border reorder")).toEqual([]);
    expect(tradingWords("research signal — not an order or investment advice")).toEqual([]);
    expect(tradingWords("research signal — not an order or investment advice; sell")).toEqual(["sell"]);
  });
});

describe("safeHttpUrl", () => {
  it("accepts only http and https", () => {
    expect(safeHttpUrl("https://a.example/x?y=1")).toBe("https://a.example/x?y=1");
    expect(safeHttpUrl("http://a.example")).toBe("http://a.example/");
    for (const bad of ["javascript:alert(1)", "data:text/html,x", "ftp://a", "//a.example", "not a url", "", null, undefined])
      expect(safeHttpUrl(bad as any)).toBeNull();
  });
});
