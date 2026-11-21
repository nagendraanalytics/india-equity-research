import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { PipelineView, buildRuns } from "../components/PipelineView";
import { TREE } from "./fixtures";
import { installFetch } from "./helpers";

const t = (min: number) => new Date(Date.now() - min * 60_000).toISOString();
const ev = (id: number, agent_id: number, kind: string, min: number) => ({ id, agent_id, kind, payload: {}, cost: 0, created_at: t(min) });

describe("buildRuns", () => {
  it("pairs started with the next completed or failed, and leaves an unfinished start running", () => {
    const runs = buildRuns([ev(4, 5, "started", 1), ev(2, 5, "failed", 8), ev(1, 5, "started", 10), ev(3, 6, "started", 2)]);
    const r5 = runs.filter((r) => r.agentId === 5).sort((a, b) => a.start - b.start);
    expect(r5.map((r) => r.outcome)).toEqual(["failed", "running"]);
    expect(runs.find((r) => r.agentId === 6)!.outcome).toBe("running");
  });
});

describe("pipeline dashboard", () => {
  const routes = () => ({ "GET /agents/tree": TREE, "GET /agents/activity": { events: [
    ev(3, 5, "completed", 4), ev(2, 5, "started", 6), ev(1, 6, "failed", 3)] } });

  it("shows stages, a run timeline, per-company worker steps and a trace for the selected agent", async () => {
    installFetch(routes());
    const user = userEvent.setup();
    render(<PipelineView pollMs={60_000} />);
    expect(await screen.findByRole("list", { name: /pipeline stages/i })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: /run timeline/i })).toBeInTheDocument();
    const table = screen.getByRole("table");
    expect(within(table).getByText("Alpha Ltd")).toBeInTheDocument();
    expect(within(table).getByText("fundamentals")).toBeInTheDocument();
    await user.click(within(table).getByText("Alpha Ltd"));
    expect(screen.getByRole("complementary", { name: /agent trace/i })).toHaveTextContent(/completed/);
  });

  it("filters the timeline to one stage and explains an empty window", async () => {
    installFetch(routes());
    const user = userEvent.setup();
    render(<PipelineView pollMs={60_000} />);
    await user.click(await screen.findByRole("button", { name: /master stage/i }));
    expect(screen.getByText(/no agent ran in this window/i)).toBeInTheDocument();
  });
});
