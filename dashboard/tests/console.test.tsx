import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ConsoleView } from "../components/ConsoleView";
import { SUMMARY, TREE, node } from "./fixtures";
import { FakeEventSource, installEventSource, installFetch } from "./helpers";

const deepClone = <T,>(x: T): T => JSON.parse(JSON.stringify(x));

function setup(routes: Record<string, unknown> = {}, log: string[] = []) {
  const ES = installEventSource();
  const f = installFetch({ "GET /agents/tree": TREE, "GET /agents/summary": SUMMARY, ...routes }, log);
  return { ES, ...f };
}

const treeCalls = (log: string[]) => log.filter((l) => l.startsWith("GET /agents/tree")).length;

describe("agent console", () => {
  it("console renders tree with status per node", async () => {
    setup();
    render(<ConsoleView />);
    // root level: the master is the header, its children are the cards
    expect(await screen.findByText("Information Technology")).toBeInTheDocument();
    expect(screen.getByText("Master Orchestrator")).toBeInTheDocument();
    expect(screen.getByTestId("status-2")).toHaveTextContent("running");
    expect(screen.getByTestId("status-20")).toHaveTextContent("failed");
    expect(screen.getByTestId("status-30")).toHaveTextContent("idle");
    expect(screen.getByTestId("status-1")).toHaveTextContent("running");
    // current task and cost per node
    expect(within(screen.getByTestId("node-2")).getByText(/sector sweep/)).toBeInTheDocument();
    expect(within(screen.getByTestId("node-2")).getByTestId("cost-2")).toHaveTextContent("2.50");
    // header strip
    expect(screen.getByTestId("queue-depth")).toHaveTextContent("3");
    expect(screen.getByTestId("compute-today")).toHaveTextContent("12345");
    expect(screen.getByTestId("failures-today")).toHaveTextContent("2");
    // freshness is shown
    expect(screen.getByTestId("freshness")).toHaveTextContent(/event #5/i);
  });

  it("shows a blocked node (waiting for its parent job) distinctly from queued", async () => {
    const tree = deepClone(TREE);
    tree.tree.children.push(node(40, 2, "Energy", "blocked", []));
    setup({ "GET /agents/tree": tree });
    render(<ConsoleView />);
    const badge = await screen.findByTestId("status-40");
    expect(badge).toHaveTextContent("blocked");
    expect(badge).toHaveClass("status-blocked");
    expect(badge).toHaveAttribute("title", expect.stringMatching(/waiting for its parent job/));
  });

  it("clicking a node drills down one level and shows its children", async () => {
    setup();
    const user = userEvent.setup();
    render(<ConsoleView />);
    await user.click(await screen.findByRole("button", { name: /open Information Technology/i }));
    // only the clicked node's children
    expect(screen.getByText("Software")).toBeInTheDocument();
    // not the siblings, the siblings' children, nor the grandchildren
    expect(screen.queryByText("Financials")).not.toBeInTheDocument();
    expect(screen.queryByText("Healthcare")).not.toBeInTheDocument();
    expect(screen.queryByText("Banks")).not.toBeInTheDocument();
    expect(screen.queryByText("IT Services")).not.toBeInTheDocument();
    // breadcrumb back up
    const crumbs = screen.getByRole("navigation", { name: /breadcrumb/i });
    expect(within(crumbs).getByText("Information Technology")).toBeInTheDocument();
    await user.click(within(crumbs).getByRole("button", { name: "Master Orchestrator" }));
    expect(screen.getByText("Financials")).toBeInTheDocument();
    expect(screen.queryByText("Software")).not.toBeInTheDocument();
    // a second drill goes one more level only
    await user.click(screen.getByRole("button", { name: /open Financials/i }));
    expect(screen.getByText("Banks")).toBeInTheDocument();
    expect(screen.queryByText("Private Banks")).not.toBeInTheDocument();
    expect(screen.queryByText("Software")).not.toBeInTheDocument();
  });

  it("new stream event updates node status without reload", async () => {
    const { ES, log } = setup();
    render(<ConsoleView />);
    await screen.findByText("Healthcare");
    await waitFor(() => expect(ES.instances.length).toBe(1));
    expect(treeCalls(log)).toBe(1);
    expect(screen.getByTestId("status-30")).toHaveTextContent("idle");

    act(() => ES.instances[0].emit({ id: 6, agent_id: 30, kind: "started", payload: { task: "scanning trials" } }));
    expect(screen.getByTestId("status-30")).toHaveTextContent("running");
    expect(within(screen.getByTestId("node-30")).getByText(/scanning trials/)).toBeInTheDocument();

    act(() => ES.instances[0].emit({ id: 7, agent_id: 30, kind: "failed", payload: { error: "boom" } }));
    expect(screen.getByTestId("status-30")).toHaveTextContent("failed");
    expect(within(screen.getByTestId("node-30")).queryByText(/scanning trials/)).not.toBeInTheDocument();

    // an event for a hidden descendant rolls up to the visible ancestor
    act(() => ES.instances[0].emit({ id: 8, agent_id: 22, kind: "started", payload: { task: "x" } }));
    expect(screen.getByTestId("status-20")).toHaveTextContent("running");

    // the same event delivered twice is idempotent; no tree refetch happened
    act(() => ES.instances[0].emit({ id: 8, agent_id: 22, kind: "started", payload: { task: "x" } }));
    expect(screen.getByTestId("status-20")).toHaveTextContent("running");
    expect(treeCalls(log)).toBe(1);
  });

  it("on reconnect console reloads tree before applying events", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const log: string[] = [];
    const ES = installEventSource();
    ES.onCreate = (es) => log.push(`EventSource ${es.url}`);
    let n = 0;
    const second = deepClone(TREE);
    second.last_event_id = 10;
    second.tree.children[2] = node(30, 2, "Healthcare", "completed");
    installFetch({
      "GET /agents/tree": () => (++n === 1 ? TREE : second),
      "GET /agents/summary": SUMMARY,
    }, log);

    render(<ConsoleView />);
    await screen.findByText("Healthcare");
    await waitFor(() => expect(ES.instances.length).toBe(1));
    expect(log.filter((l) => !l.includes("summary"))).toEqual([
      "GET /agents/tree",
      expect.stringMatching(/^EventSource .*\/stream\?last_event_id=5$/),
    ]);

    // the connection drops
    act(() => ES.instances[0].fail());
    expect(ES.instances[0].closed).toBe(true);
    await act(async () => { await vi.advanceTimersByTimeAsync(1500); });
    await waitFor(() => expect(ES.instances.length).toBe(2));

    // tree fetched BEFORE the second stream was constructed, and the stream resumes at the
    // tree's last_event_id
    const calls = log.filter((l) => !l.includes("summary"));
    expect(calls).toEqual([
      "GET /agents/tree",
      expect.stringMatching(/stream\?last_event_id=5$/),
      "GET /agents/tree",
      expect.stringMatching(/stream\?last_event_id=10$/),
    ]);
    expect(screen.getByTestId("status-30")).toHaveTextContent("completed");

    // events at or below the tree's last_event_id are ignored; newer ones apply
    act(() => ES.instances[1].emit({ id: 9, agent_id: 30, kind: "started", payload: { task: "stale replay" } }));
    act(() => ES.instances[1].emit({ id: 10, agent_id: 30, kind: "failed", payload: {} }));
    expect(screen.getByTestId("status-30")).toHaveTextContent("completed");
    act(() => ES.instances[1].emit({ id: 11, agent_id: 30, kind: "started", payload: { task: "fresh" } }));
    expect(screen.getByTestId("status-30")).toHaveTextContent("running");
    // the dead first connection can no longer change anything
    act(() => ES.instances[0].emit({ id: 12, agent_id: 30, kind: "failed", payload: {} }));
    expect(screen.getByTestId("status-30")).toHaveTextContent("running");
  });

  it("console resyncs the tree every 30 seconds before reopening the stream", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const log: string[] = [];
    const ES = installEventSource();
    ES.onCreate = (es) => log.push(`EventSource ${es.url}`);
    installFetch({ "GET /agents/tree": TREE, "GET /agents/summary": SUMMARY }, log);
    render(<ConsoleView />);
    await screen.findByText("Healthcare");
    await waitFor(() => expect(ES.instances.length).toBe(1));
    await act(async () => { await vi.advanceTimersByTimeAsync(29_000); });
    expect(treeCalls(log)).toBe(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(2_000); });
    await waitFor(() => expect(ES.instances.length).toBe(2));
    expect(treeCalls(log)).toBe(2);
    const calls = log.filter((l) => !l.includes("summary"));
    expect(calls.indexOf("GET /agents/tree", 1)).toBeLessThan(calls.findIndex((l, i) => i > 1 && l.startsWith("EventSource")));
    expect(ES.instances[0].closed).toBe(true);
  });

  it("re-run button posts and reports 200, 409 and 404 visibly", async () => {
    let status = 200;
    const { log } = setup({
      "POST /agents/2/rerun": () => status === 200
        ? { job_id: 9, agent_id: 2, already_queued: false, event_id: 12 }
        : { status, body: { detail: status === 409 ? "agent 2 (role 'macro_sector') cannot be rerun" : "unknown agent id 2" } },
    });
    const user = userEvent.setup();
    render(<ConsoleView />);
    const btn = await screen.findByRole("button", { name: /re-run Information Technology/i });
    await user.click(btn);
    expect(log).toContain("POST /agents/2/rerun");
    expect(await screen.findByRole("status")).toHaveTextContent(/queued.*job 9/i);
    // the click did not drill down
    expect(screen.queryByText("Software")).not.toBeInTheDocument();
    status = 409;
    await user.click(btn);
    expect(await screen.findByText(/cannot be rerun/)).toBeInTheDocument();
    status = 404;
    await user.click(btn);
    expect(await screen.findByText(/unknown agent id 2/)).toBeInTheDocument();
  });

  it("shows a visible error and no blank screen when the tree cannot be loaded", async () => {
    setup({ "GET /agents/tree": { status: 404, body: { detail: "no master agent; run seed_agents first" } } });
    render(<ConsoleView />);
    expect(await screen.findByRole("alert")).toHaveTextContent(/no master agent/);
  });

  it("renders text from the API as text, not markup", async () => {
    const evil = deepClone(TREE);
    evil.tree.children[0].name = "<img src=x onerror=alert(1)>";
    setup({ "GET /agents/tree": evil });
    const { container } = render(<ConsoleView />);
    expect(await screen.findByText("<img src=x onerror=alert(1)>")).toBeInTheDocument();
    expect(container.querySelector("img")).toBeNull();
  });

  it("drill-down recovers when the focused node disappears after a resync", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    installEventSource();
    let n = 0;
    const second = deepClone(TREE);
    // Software (3) vanishes from IT; IT now has a different child with its own child
    second.tree.children[0].children = [node(40, 3, "Cloud", "idle", [node(41, 4, "Hosting", "idle")])];
    installFetch({ "GET /agents/tree": () => (++n === 1 ? TREE : second), "GET /agents/summary": SUMMARY });
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    render(<ConsoleView />);
    await user.click(await screen.findByRole("button", { name: /open Information Technology/i }));
    await user.click(await screen.findByRole("button", { name: /open Software/i }));
    expect(screen.getByText("IT Services")).toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(31_000); });
    // falls back to the nearest ancestor that still exists (Information Technology)
    const crumbs = await screen.findByRole("navigation", { name: /breadcrumb/i });
    expect(await screen.findByText("Cloud")).toBeInTheDocument();
    expect(within(crumbs).getByRole("button", { name: "Master Orchestrator" })).toBeInTheDocument();
    expect(within(crumbs).getByText("Information Technology")).toBeInTheDocument();
    // and Open on a visible child works (not stuck on a stale path)
    await user.click(screen.getByRole("button", { name: /open Cloud/i }));
    expect(screen.getByText("Hosting")).toBeInTheDocument();
    expect(within(screen.getByRole("navigation", { name: /breadcrumb/i })).getByText("Cloud")).toBeInTheDocument();
  });

  it("re-run is disabled while its request is in flight and a second click sends no second POST", async () => {
    let release!: (v: unknown) => void;
    const pending = new Promise((r) => { release = r; });
    let posts = 0;
    setup({ "POST /agents/2/rerun": async () => { posts += 1; await pending; return { job_id: 4, agent_id: 2, already_queued: false, event_id: 1 }; } });
    const user = userEvent.setup();
    render(<ConsoleView />);
    const btn = await screen.findByRole("button", { name: /re-run Information Technology/i });
    await user.click(btn);
    await user.click(btn);
    expect(btn).toBeDisabled();
    expect(posts).toBe(1);
    await act(async () => { release(null); });
    expect(await screen.findByRole("status")).toHaveTextContent(/job 4/);
    expect(btn).toBeEnabled();
  });

  it("a new re-run clears the previous message for that node", async () => {
    let ok = false;
    setup({ "POST /agents/2/rerun": () => (ok ? { job_id: 5, agent_id: 2, already_queued: false, event_id: 1 }
      : { status: 409, body: { detail: "old failure text" } }) });
    const user = userEvent.setup();
    render(<ConsoleView />);
    const btn = await screen.findByRole("button", { name: /re-run Information Technology/i });
    await user.click(btn);
    expect(await screen.findByText(/old failure text/)).toBeInTheDocument();
    ok = true;
    await user.click(btn);
    expect(await screen.findByRole("status")).toHaveTextContent(/job 5/);
    expect(screen.queryByText(/old failure text/)).not.toBeInTheDocument();
  });

  it("dedupes by event id: a replayed older event cannot undo a newer one", async () => {
    const { ES } = setup();
    render(<ConsoleView />);
    await screen.findByText("Healthcare");
    await waitFor(() => expect(ES.instances.length).toBe(1));
    act(() => ES.instances[0].emit({ id: 8, agent_id: 30, kind: "started", payload: { task: "t" } }));
    act(() => ES.instances[0].emit({ id: 9, agent_id: 30, kind: "completed" }));
    expect(screen.getByTestId("status-30")).toHaveTextContent("completed");
    act(() => ES.instances[0].emit({ id: 8, agent_id: 30, kind: "started", payload: { task: "t" } }));
    expect(screen.getByTestId("status-30")).toHaveTextContent("completed");
  });

  it("frames already in flight on a closed connection are ignored", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const ES = installEventSource();
    installFetch({ "GET /agents/tree": TREE, "GET /agents/summary": SUMMARY });
    render(<ConsoleView />);
    await screen.findByText("Healthcare");
    await waitFor(() => expect(ES.instances.length).toBe(1));
    act(() => ES.instances[0].fail());
    await act(async () => { await vi.advanceTimersByTimeAsync(1500); });
    await waitFor(() => expect(ES.instances.length).toBe(2));
    expect(ES.instances[0].closed).toBe(true);
    act(() => ES.instances[0].deliverEvenIfClosed({ id: 50, agent_id: 30, kind: "failed" }));
    expect(screen.getByTestId("status-30")).toHaveTextContent("idle");
  });

  it("when overlapping tree fetches finish out of order the newest request wins", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const ES = installEventSource();
    let n = 0;
    let releaseOld!: (v: unknown) => void;
    const oldTree = deepClone(TREE);              // what the slow, older request will return
    const newTree = deepClone(TREE);
    newTree.last_event_id = 20;
    newTree.tree.children[2] = node(30, 2, "Healthcare", "completed");
    installFetch({
      "GET /agents/tree": () => {
        n += 1;
        if (n === 1) return TREE;
        if (n === 2) return new Promise((r) => { releaseOld = () => r(oldTree); });   // slow, older
        return newTree;                                                              // fast, newer
      },
      "GET /agents/summary": SUMMARY,
    });
    render(<ConsoleView />);
    await screen.findByText("Healthcare");
    await waitFor(() => expect(ES.instances.length).toBe(1));
    await act(async () => { await vi.advanceTimersByTimeAsync(30_500); });   // request 2 hangs
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });   // request 3 wins
    await waitFor(() => expect(screen.getByTestId("status-30")).toHaveTextContent("completed"));
    const before = ES.instances.length;
    await act(async () => { releaseOld(null); });                            // the old one arrives last
    expect(screen.getByTestId("status-30")).toHaveTextContent("completed");
    expect(ES.instances.length).toBe(before);                                // and opens no stream
    expect(ES.instances.at(-1)!.url).toMatch(/last_event_id=20$/);
  });
});
