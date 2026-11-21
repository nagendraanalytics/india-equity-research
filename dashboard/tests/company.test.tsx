import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { CompanyView, flowStep } from "../components/CompanyView";
import { node, TREE } from "./fixtures";
import { installFetch } from "./helpers";

const DETAIL = { id: 5, tier: 5, tier_name: "company", role: "company", name: "Alpha Ltd", parent_id: 4, scope_sector: "Software",
  isin: "INE000A01011", status: "running", current_task: null, last_run: null, rerunnable: true,
  cost: { cost_today: 1.5, event_cost_total: 3, total_tokens: 900, model_seconds: 4 }, last_error: null };
const STOCK = { isin: "INE000A01011", name: "Alpha Ltd", long_term_score: 61.2, short_term_score: null, confidence: "medium", rating: "hold" };

describe("company agents drill-down", () => {
  it("shows built agents with live status, planned ones as not built, and the score", async () => {
    installFetch({ "GET /agents/tree": TREE, "GET /agents/activity": { events: [] }, "GET /agents/5": DETAIL,
      "GET /stocks/INE000A01011": STOCK, "GET /stocks/INE000A01011/history": { items: [] } });
    render(<CompanyView agentId={5} pollMs={60_000} />);
    expect(await screen.findByRole("heading", { name: /Alpha Ltd/ })).toBeInTheDocument();
    expect(screen.queryByTestId("roster-verifier")).not.toBeInTheDocument();   // verification runs inside Scoring
    expect(within(screen.getByTestId("roster-fundamentals")).getByText("running")).toBeInTheDocument();
    expect(within(screen.getByTestId("roster-fundamentals")).getByText(/reading filings/)).toBeInTheDocument();
    // feed agents exist; with no files they say what to drop and never claim data
    for (const k of ["news", "social", "technicals"]) expect(within(screen.getByTestId(`roster-${k}`)).getByText(/in this company's folder/)).toBeInTheDocument();
    expect(screen.getByTestId("co-active")).toHaveTextContent("1");
    expect((await screen.findAllByText("61.2")).length).toBeGreaterThan(0);
  });

  it("reports an unknown company instead of an empty page", async () => {
    installFetch({ "GET /agents/tree": TREE, "GET /agents/activity": { events: [] }, "GET /agents/999": { ...DETAIL, id: 999 } });
    render(<CompanyView agentId={999} pollMs={60_000} />);
    expect(await screen.findByRole("alert")).toHaveTextContent(/not in the tree/);
  });
});

describe("flowStep", () => {
  const w = (name: string, status: string) => node(1, 6, name, status);
  it("tracks where a change is in the flow", () => {
    expect(flowStep([w("fundamentals", "running")], "running")).toBe(1);
    expect(flowStep([w("fundamentals", "completed"), w("synthesis", "running")], "running")).toBe(2);
    expect(flowStep([w("synthesis", "completed")], "completed")).toBe(3);
    expect(flowStep([], "queued")).toBe(0);
  });
});

describe("earlier failure", () => {
  it("is shown as recovered, not as an alert, once the company has completed", async () => {
    const done = { ...TREE, tree: node(5, 5, "Alpha Ltd", "completed", []) };
    installFetch({ "GET /agents/tree": done, "GET /agents/activity": { events: [] },
      "GET /agents/5": { ...DETAIL, status: "completed", last_error: { message: "401 Invalid API Key" } }, "GET /stocks/INE000A01011": STOCK, "GET /stocks/INE000A01011/history": { items: [] } });
    render(<CompanyView agentId={5} pollMs={60_000} />);
    expect(await screen.findByText(/since recovered/)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});

describe("feed agents on the company page", () => {
  it("shows technicals and the news events with sentiment, and marks social as unverified", async () => {
    const tree = { last_event_id: 1, tree: node(5, 5, "Alpha Ltd", "completed", [
      node(6, 6, "technicals", "completed"), node(7, 6, "news", "completed"), node(8, 6, "social", "completed")]) };
    const out = (payload: unknown) => ({ ...DETAIL, id: 0, last_output: { event_id: 1, created_at: null, payload } });
    installFetch({ "GET /agents/tree": tree, "GET /agents/activity": { events: [] }, "GET /agents/5": DETAIL,
      "GET /agents/6": out({ files: 1, as_of: "2026-11-13", metrics: { price_vs_sma200: 12.34, momentum_3m: 5.5, rsi14: 61.2 } }),
      "GET /agents/7": out({ kind: "news_view", files: ["n.txt"], items: [{ headline: "Buyback approved", quote: "q", file: "n.txt", event_type: "deal", sentiment: "positive", severity: "medium", verified: true }] }),
      "GET /agents/8": out({ kind: "social_view", files: ["s.txt"], items: [] }),
      "GET /stocks/INE000A01011": STOCK, "GET /stocks/INE000A01011/history": { items: [] } });
    render(<CompanyView agentId={5} pollMs={60_000} />);
    expect(await screen.findByText("12.3%")).toBeInTheDocument();
    expect(screen.getByText("2026-11-13")).toBeInTheDocument();
    expect(within(screen.getByTestId("roster-news")).getByText("Buyback approved")).toBeInTheDocument();
    expect(within(screen.getByTestId("roster-news")).getByText("positive")).toBeInTheDocument();
    expect(within(screen.getByTestId("roster-social")).getByText(/unverified/)).toBeInTheDocument();
  });
});
