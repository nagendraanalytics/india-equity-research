import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { FleetView, flatten } from "../components/FleetView";
import { ConsoleView, CHILD_PAGE } from "../components/ConsoleView";
import { SUMMARY, TREE, node } from "./fixtures";
import { installEventSource, installFetch } from "./helpers";

const METRICS = {
  events_by_minute: Array.from({ length: 60 }, (_, i) => ({ t: new Date(Date.UTC(2026, 10, 14, 10, i)).toISOString(), started: i % 3, completed: i % 2, failed: i === 59 ? 1 : 0 })),
  tokens_by_hour: Array.from({ length: 24 }, (_, i) => ({ t: new Date(Date.UTC(2026, 10, 13, 10 + (i % 14), 0)).toISOString(), tokens: i * 1000, model_seconds: i })),
  jobs_by_status: { queued: 3, running: 1, done: 9, failed: 2 },
  recent_failures: [{ event_id: 9, agent_id: 5, created_at: null, error: "boom" }],
  last_event_id: 5,
};

const big = (n: number) => node(1, 1, "Master Orchestrator", "running", [
  node(2, 2, "Information Technology", "running",
    Array.from({ length: n }, (_, i) => node(100 + i, 5, `Company ${i}`, i % 7 === 0 ? "failed" : i % 3 === 0 ? "running" : "completed", [], { cost_today: i % 10 }))),
]);

describe("fleet dashboard", () => {
  it("counts agents by status and shows the charts", async () => {
    installFetch({ "GET /agents/tree": TREE, "GET /agents/summary": SUMMARY, "GET /agents/metrics": METRICS });
    render(<FleetView pollMs={60_000} />);
    expect(await screen.findByTestId("fleet-total")).toHaveTextContent(String(flatten(TREE.tree).length));
    expect(screen.getByRole("img", { name: /agents by status/i })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: /events per minute/i })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: /tokens per hour/i })).toBeInTheDocument();
    expect(screen.getByRole("group", { name: /fleet map/i })).toBeInTheDocument();
  });

  it("stays responsive with 2500 agents: the list renders a window, the filter narrows it, a row opens details", async () => {
    installFetch({ "GET /agents/tree": { last_event_id: 1, tree: big(2500) }, "GET /agents/summary": SUMMARY, "GET /agents/metrics": METRICS });
    const user = userEvent.setup();
    render(<FleetView pollMs={60_000} />);
    expect(await screen.findByTestId("fleet-total")).toHaveTextContent("2502");
    const list = screen.getByTestId("fleet-list");
    expect(within(list).getAllByRole("listitem").length).toBeLessThan(40);   // windowed, not 2500 rows
    await user.type(screen.getByLabelText("Search"), "Company 1234");
    expect(screen.getByTestId("fleet-matching")).toHaveTextContent("1 of 2502");
    await user.click(await screen.findByTestId("fleet-row-1334"));
    expect(screen.getByRole("complementary", { name: /selected agent/i })).toHaveTextContent("Company 1234");
  });

  it("shows a refresh error without dropping the data already on screen", async () => {
    let calls = 0;
    installFetch({ "GET /agents/tree": () => (++calls > 3 ? { status: 500, body: { detail: "db down" } } : TREE),
                   "GET /agents/summary": SUMMARY, "GET /agents/metrics": METRICS });
    render(<FleetView pollMs={30} />);
    expect(await screen.findByRole("alert")).toHaveTextContent(/refresh failed/i);
    expect(screen.getByTestId("fleet-total")).toBeInTheDocument();
  });
});

describe("console with many children", () => {
  it("draws one page of children and reveals the rest on request", async () => {
    installEventSource();
    installFetch({ "GET /agents/tree": { last_event_id: 1, tree: big(150) }, "GET /agents/summary": SUMMARY });
    const user = userEvent.setup();
    render(<ConsoleView />);
    await user.click(await screen.findByRole("button", { name: /open Information Technology/i }));
    expect(screen.getAllByTestId(/^node-\d+$/)).toHaveLength(CHILD_PAGE);
    await user.click(screen.getByRole("button", { name: /show more/i }));
    expect(screen.getAllByTestId(/^node-\d+$/)).toHaveLength(CHILD_PAGE * 2);
  });
});

describe("fleet with an older API", () => {
  it("still shows agents when /agents/metrics is missing", async () => {
    installFetch({ "GET /agents/tree": TREE, "GET /agents/summary": SUMMARY,
                   "GET /agents/metrics": { status: 422, body: { detail: "Input should be a valid integer" } } });
    render(<FleetView pollMs={60_000} />);
    expect(await screen.findByTestId("fleet-total")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(/restart the api/i);
  });
});
