"use client";

import { GraphEdge, GraphNode } from "@/lib/api";

/**
 * Layered DAG rendering for a workflow subgraph.
 *
 * Hand-rolled SVG rather than a graph library: these workflows are small
 * (typically 3-10 nodes), the visual language has to match the existing
 * case-file styling, and a library would bring its own conventions to
 * override. Revisit if graphs grow past ~30 nodes or need pan/zoom.
 *
 * Layer assignment is longest-path-from-source. The backend's
 * traverse_from walks edges in both directions, so the returned subgraph
 * can contain cycles even though workflows are conceptually DAGs --
 * the traversal below is explicitly cycle-safe rather than assuming
 * acyclicity and hanging if it's violated.
 */

const NODE_W = 168;
const NODE_H = 52;
const H_GAP = 60;
const V_GAP = 28;
const PAD = 24;

type Positioned = GraphNode & { x: number; y: number; layer: number };

function assignLayers(nodes: GraphNode[], edges: GraphEdge[]): Map<string, number> {
  const ids = new Set(nodes.map((n) => n.id));
  const outgoing = new Map<string, string[]>();
  const indegree = new Map<string, number>();

  for (const id of ids) {
    outgoing.set(id, []);
    indegree.set(id, 0);
  }
  for (const e of edges) {
    if (!ids.has(e.source) || !ids.has(e.target)) continue;
    outgoing.get(e.source)!.push(e.target);
    indegree.set(e.target, (indegree.get(e.target) ?? 0) + 1);
  }

  const layer = new Map<string, number>();
  for (const id of ids) layer.set(id, 0);

  // Kahn's algorithm. Any node left unprocessed is part of a cycle and
  // keeps its default layer 0 -- degraded but still renders, rather than
  // looping forever.
  const queue = [...ids].filter((id) => (indegree.get(id) ?? 0) === 0);
  const remaining = new Map(indegree);
  let processed = 0;

  while (queue.length > 0) {
    const id = queue.shift()!;
    processed++;
    for (const next of outgoing.get(id) ?? []) {
      layer.set(next, Math.max(layer.get(next) ?? 0, (layer.get(id) ?? 0) + 1));
      const left = (remaining.get(next) ?? 1) - 1;
      remaining.set(next, left);
      if (left === 0) queue.push(next);
    }
  }

  if (processed < ids.size) {
    // Cyclic subgraph: fall back to BFS depth from an arbitrary root so
    // the layout is still readable rather than collapsing to one column.
    const seen = new Set<string>();
    const start = [...ids][0];
    const bfs: [string, number][] = [[start, 0]];
    while (bfs.length > 0) {
      const [id, d] = bfs.shift()!;
      if (seen.has(id)) continue;
      seen.add(id);
      layer.set(id, d);
      for (const next of outgoing.get(id) ?? []) {
        if (!seen.has(next)) bfs.push([next, d + 1]);
      }
    }
  }

  return layer;
}

function layout(nodes: GraphNode[], edges: GraphEdge[]): {
  positioned: Positioned[];
  width: number;
  height: number;
} {
  const layerOf = assignLayers(nodes, edges);
  const byLayer = new Map<number, GraphNode[]>();
  for (const n of nodes) {
    const l = layerOf.get(n.id) ?? 0;
    if (!byLayer.has(l)) byLayer.set(l, []);
    byLayer.get(l)!.push(n);
  }

  const layers = [...byLayer.keys()].sort((a, b) => a - b);
  const tallest = Math.max(...layers.map((l) => byLayer.get(l)!.length), 1);
  const height = PAD * 2 + tallest * NODE_H + (tallest - 1) * V_GAP;

  const positioned: Positioned[] = [];
  layers.forEach((l, columnIndex) => {
    const column = byLayer.get(l)!;
    const columnHeight = column.length * NODE_H + (column.length - 1) * V_GAP;
    const yStart = (height - columnHeight) / 2;
    column.forEach((n, i) => {
      positioned.push({
        ...n,
        layer: l,
        x: PAD + columnIndex * (NODE_W + H_GAP),
        y: yStart + i * (NODE_H + V_GAP),
      });
    });
  });

  const width = PAD * 2 + layers.length * NODE_W + (layers.length - 1) * H_GAP;
  return { positioned, width, height };
}

export default function WorkflowGraph({
  nodes,
  edges,
  center,
}: {
  nodes: GraphNode[];
  edges: GraphEdge[];
  center: string;
}) {
  if (nodes.length === 0) {
    return <p className="case-body">No connected nodes to display.</p>;
  }

  const { positioned, width, height } = layout(nodes, edges);
  const posById = new Map(positioned.map((p) => [p.id, p]));

  return (
    <div style={{ overflowX: "auto" }}>
      <svg
        width={width}
        height={height}
        viewBox={`0 0 ${width} ${height}`}
        style={{ maxWidth: "100%" }}
        role="img"
        aria-label="Workflow task graph"
      >
        <defs>
          <marker
            id="arrow"
            viewBox="0 0 10 10"
            refX="9"
            refY="5"
            markerWidth="6"
            markerHeight="6"
            orient="auto-start-reverse"
          >
            <path d="M 0 0 L 10 5 L 0 10 z" fill="var(--rule-paper)" />
          </marker>
        </defs>

        {edges.map((e) => {
          const s = posById.get(e.source);
          const t = posById.get(e.target);
          if (!s || !t) return null;

          // Draw left-to-right regardless of stored direction, so arrows
          // read naturally against the layered layout.
          const forward = s.x <= t.x;
          const from = forward ? s : t;
          const to = forward ? t : s;
          const x1 = from.x + NODE_W;
          const y1 = from.y + NODE_H / 2;
          const x2 = to.x;
          const y2 = to.y + NODE_H / 2;
          const midX = (x1 + x2) / 2;

          return (
            <g key={e.id}>
              <path
                d={`M ${x1} ${y1} C ${midX} ${y1}, ${midX} ${y2}, ${x2} ${y2}`}
                fill="none"
                stroke="var(--rule-paper)"
                strokeWidth="1.5"
                markerEnd="url(#arrow)"
              />
              <text
                x={midX}
                y={(y1 + y2) / 2 - 6}
                textAnchor="middle"
                fontFamily="var(--font-mono)"
                fontSize="9"
                fill="var(--paper-text-dim)"
              >
                {e.label}
              </text>
            </g>
          );
        })}

        {positioned.map((n) => {
          const isCenter = n.id === center;
          const isTask = n.table === "task_nodes";
          return (
            <g key={n.id}>
              <rect
                x={n.x}
                y={n.y}
                width={NODE_W}
                height={NODE_H}
                rx={3}
                fill={isCenter ? "var(--paper-dim)" : "transparent"}
                stroke={isCenter ? "var(--pass)" : "var(--rule-paper)"}
                strokeWidth={isCenter ? 2 : 1}
                strokeDasharray={isTask ? undefined : "4 3"}
              />
              <text
                x={n.x + NODE_W / 2}
                y={n.y + 20}
                textAnchor="middle"
                fontFamily="var(--font-mono)"
                fontSize="8"
                fill="var(--paper-text-dim)"
                letterSpacing="0.05em"
              >
                {isTask ? "TASK" : "KNOWLEDGE"}
              </text>
              <text
                x={n.x + NODE_W / 2}
                y={n.y + 37}
                textAnchor="middle"
                fontFamily="var(--font-serif)"
                fontSize="13"
                fill="var(--paper-text)"
              >
                {n.label.length > 22 ? `${n.label.slice(0, 21)}…` : n.label}
              </text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}
