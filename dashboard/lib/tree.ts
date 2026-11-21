import type { AgentEvent, TreeNode } from "./types";

const KIND_STATUS: Record<string, string> = {
  queued: "queued", started: "running", completed: "completed", failed: "failed",
};

export const TIER_LABELS = ["master", "macro sector", "sector", "industry", "company", "worker", "segment", "source"];
export const tierLabel = (tier: number) => TIER_LABELS[tier - 1] ?? `tier ${tier}`;

export function findPath(root: TreeNode, id: number): TreeNode[] | null {
  if (root.id === id) return [root];
  for (const c of root.children) {
    const p = findPath(c, id);
    if (p) return [root, ...p];
  }
  return null;
}

/** Follow `path` (node ids below the root) as far as it exists. */
export function resolveFocus(root: TreeNode, path: number[]): TreeNode[] {
  const chain = [root];
  for (const id of path) {
    const next = chain[chain.length - 1].children.find((c) => c.id === id);
    if (!next) break;
    chain.push(next);
  }
  return chain;
}

/** Apply one stream event (pure, idempotent). Status events set the agent's status and current
 *  task; a `started` also marks every ancestor running. Other rollups are left to the next
 *  GET /agents/tree resync (the tree is the source of truth, the stream only lowers latency). */
export function applyEvent(root: TreeNode, ev: Pick<AgentEvent, "agent_id" | "kind" | "payload" | "created_at">): TreeNode {
  const status = KIND_STATUS[ev.kind];
  if (!status) return root;
  const path = findPath(root, ev.agent_id);
  if (!path) return root;
  const task = ev.kind === "started" && typeof (ev.payload as any)?.task === "string" ? (ev.payload as any).task : null;
  const rebuild = (i: number): TreeNode => {
    const n = path[i];
    if (i === path.length - 1) {
      return { ...n, status, current_task: ev.kind === "started" ? task : null,
               last_run: ev.kind === "completed" || ev.kind === "failed" ? ev.created_at ?? n.last_run : n.last_run };
    }
    const next = rebuild(i + 1);
    return { ...n, status: ev.kind === "started" ? "running" : n.status,
             children: n.children.map((c) => (c.id === next.id ? next : c)) };
  };
  return rebuild(0);
}
