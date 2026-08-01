/**
 * Convert a generated change set into the shape WorkflowGraph renders.
 *
 * Generated ops carry local `ref` strings rather than database ids —
 * the nodes don't exist yet, which is the whole point of a proposal. The
 * refs are stable and unique within a change set, so they serve as ids
 * for rendering purposes without inventing anything.
 *
 * Kept as a pure function rather than folded into the page so it can be
 * tested directly: the mapping is where a silent mismatch between the
 * backend's op vocabulary and the frontend's rendering would hide.
 */

import { GraphEdge, GraphNode } from "@/lib/api";

type Op = Record<string, unknown>;

export function opsToGraph(ops: Op[]): { nodes: GraphNode[]; edges: GraphEdge[] } {
  const nodes: GraphNode[] = [];
  const edges: GraphEdge[] = [];

  for (const op of ops) {
    const opType = op.op_type as string;

    if (opType === "create_task_node" || opType === "create_knowledge_node") {
      const ref = op.ref as string;
      if (!ref) continue;
      nodes.push({
        id: ref,
        table: opType === "create_task_node" ? "task_nodes" : "knowledge_nodes",
        label: (op.name as string) ?? ref,
      });
    }
  }

  const known = new Set(nodes.map((n) => n.id));

  ops.forEach((op, i) => {
    if (op.op_type !== "create_edge") return;
    const source = (op.source_ref as string) ?? (op.source_id as string);
    const target = (op.target_ref as string) ?? (op.target_id as string);
    // An edge referencing something outside this change set can't be
    // drawn. The backend rejects those, so reaching here means a
    // mismatch worth not rendering silently wrong.
    if (!source || !target || !known.has(source) || !known.has(target)) return;
    edges.push({
      id: `edge-${i}`,
      source,
      target,
      label: (op.edge_type as string) ?? "PRODUCES",
    });
  });

  return { nodes, edges };
}
