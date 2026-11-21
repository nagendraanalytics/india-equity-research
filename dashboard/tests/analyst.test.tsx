import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { AnalystView } from "../components/AnalystView";
import { ANALYST } from "./fixtures";
import { installFetch } from "./helpers";

describe("analyst view", () => {
  it("analyst view lists cited evidence links", async () => {
    const { log } = installFetch({ "GET /analysts/5": ANALYST });
    render(<AnalystView agentId={5} />);
    expect(await screen.findByRole("heading", { name: /Alpha Ltd/ })).toBeInTheDocument();
    expect(log).toContain("GET /analysts/5");

    // every http(s) evidence url is rendered as a safe link
    for (const url of ["https://www.bseindia.com/filing/alpha-fy25.pdf", "http://example.org/news/alpha-contract"]) {
      const a = screen.getByRole("link", { name: url });
      expect(a).toHaveAttribute("href", url);
      expect(a.getAttribute("rel")).toMatch(/noopener/);
      expect(a.getAttribute("rel")).toMatch(/noreferrer/);
    }
    const ev = screen.getAllByTestId("evidence-item");
    expect(ev).toHaveLength(3);
    expect(within(ev[0]).getByText(/tier 1/i)).toBeInTheDocument();
    expect(within(ev[0]).getByText(/2025-05-10/)).toBeInTheDocument();
    expect(within(ev[0]).getByText(/^type: filing/i)).toBeInTheDocument();
    expect(within(ev[0]).getByText(/revenue/)).toBeInTheDocument();
    // a javascript: url is shown as plain text, never as a link
    expect(screen.queryByRole("link", { name: /javascript:/ })).not.toBeInTheDocument();
    expect(document.querySelector('a[href^="javascript"]')).toBeNull();
    expect(within(ev[2]).getByText(/javascript:alert\(1\)/)).toBeInTheDocument();
  });

  it("shows findings, red flags, confidence, freshness, run history and rule inputs", async () => {
    installFetch({ "GET /analysts/5": ANALYST });
    render(<AnalystView agentId={5} />);
    await screen.findByRole("heading", { name: /Alpha Ltd/ });
    expect(screen.getByText("Auditor resigned in FY25")).toBeInTheDocument();
    expect(screen.getByText("Strong cash conversion")).toBeInTheDocument();
    expect(screen.getByText("Customer concentration")).toBeInTheDocument();
    expect(screen.getByTestId("confidence")).toHaveTextContent("high");
    expect(screen.getByTestId("freshness")).toHaveTextContent(/as of 2026-11-06/i);
    expect(screen.getByText("run-2026-11-05")).toBeInTheDocument();
    expect(screen.getByText("qwen-x")).toBeInTheDocument();
    const inputs = screen.getByTestId("rule-inputs");
    expect(inputs).toHaveTextContent("rules-v1");
    expect(inputs).toHaveTextContent("ALPHA");
  });

  it("shows no-data and error states", async () => {
    installFetch({ "GET /analysts/5": { ...ANALYST, findings: null, confidence: null, evidence: [], run_history: [],
      freshness: { as_of: null, age_days: null, stale: true } } });
    const { unmount } = render(<AnalystView agentId={5} />);
    expect(await screen.findByText(/no scorecard yet/i)).toBeInTheDocument();
    expect(screen.getByTestId("freshness")).toHaveTextContent(/no data/i);
    unmount();
    installFetch({ "GET /analysts/5": { status: 404, body: { detail: "agent 5 is not a company or worker analyst" } } });
    render(<AnalystView agentId={5} />);
    expect(await screen.findByRole("alert")).toHaveTextContent(/not a company or worker analyst/);
  });
});
