"""
GraphStore (MVP plan, Section 3.2).

Every traversal call in the application goes through this interface. The
current implementation uses Postgres recursive CTEs, which suits the
shallow entrypoint-then-expand retrieval pattern in Section 3.1 -- this
is a relational-shaped workload, not a deep-multi-hop one. If a dedicated
graph DB is ever warranted (Section 12 trigger: traversal depth or graph
size outgrowing CTE performance), it becomes a second implementation of
this same class and nothing outside this file changes.

Enum array parameters are cast to text[] rather than the native enum
array type: asyncpg requires custom enum types be registered per
connection, and passing list[str] to an enum[] parameter fails at bind
time. Comparing `edge_type::text` avoids that entirely.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional, Sequence
from uuid import UUID

import asyncpg

from app.models.ontology import EdgeType, NodeTable
from app.services.access import AccessScope, visibility_predicate

_EDGE_COLS = """
    id, edge_type::text AS edge_type, custom_edge_type,
    source_id, source_table, target_id, target_table, properties
"""


@dataclass(frozen=True)
class GraphEdge:
    id: UUID
    edge_type: str
    custom_edge_type: Optional[str]
    source_id: UUID
    source_table: str
    target_id: UUID
    target_table: str
    properties: dict[str, Any]

    def other_end(self, node_id: UUID, node_table: str) -> tuple[UUID, str]:
        """Return whichever end of this edge is not (node_id, node_table)."""
        if self.source_id == node_id and self.source_table == node_table:
            return self.target_id, self.target_table
        return self.source_id, self.source_table


def _row_to_edge(row: asyncpg.Record) -> GraphEdge:
    d = dict(row)
    # Defensive: if the JSONB codec (db/session.py) was not registered on
    # this pool, properties arrives as str. Fail loudly rather than
    # propagating a string where a dict is expected.
    if isinstance(d.get("properties"), str):
        raise RuntimeError(
            "JSONB decoded as str -- pool was created without the codec from "
            "app.db.session.create_pool()."
        )
    return GraphEdge(**d)


class GraphStore:
    """
    All traversal goes through here (Section 3.2), and as of V2 all
    traversal is access-scoped.

    The scope is taken at construction rather than per-call: a store
    built for one viewer cannot accidentally serve another's private
    content because a single call site forgot to pass it. Forgetting is
    the realistic failure mode, so the design removes the opportunity.
    """

    def __init__(self, pool: asyncpg.Pool, scope: Optional[AccessScope] = None):
        self._pool = pool
        # Default unrestricted preserves V1 behaviour for internal
        # callers (backfills, maintenance). Request paths must pass a
        # real scope -- see app/api/deps.py.
        self._scope = scope or AccessScope.unrestricted()

    async def get_neighbors(
        self,
        node_id: UUID,
        node_table: NodeTable,
        edge_types: Optional[Sequence[EdgeType]] = None,
        as_of: Optional[datetime] = None,
    ) -> list[GraphEdge]:
        """One-hop lookup, filtered to edges valid at `as_of` (default now)."""
        vis_sql, vis_params = visibility_predicate(self._scope, param_index=5)
        query = f"""
            SELECT {_EDGE_COLS}
            FROM edges
            WHERE (
                    (source_id = $1 AND source_table = $2)
                 OR (target_id = $1 AND target_table = $2)
                  )
              AND t_valid <= COALESCE($3::timestamptz, now())
              AND (t_invalid IS NULL OR t_invalid > COALESCE($3::timestamptz, now()))
              AND ($4::text[] IS NULL OR edge_type::text = ANY($4::text[]))
              AND {vis_sql}
        """
        types = list(edge_types) if edge_types else None
        rows = await self._pool.fetch(query, node_id, node_table, as_of, types, *vis_params)
        return [_row_to_edge(r) for r in rows]

    async def traverse_from(
        self,
        entrypoint_ids: Sequence[UUID],
        entrypoint_table: NodeTable,
        max_depth: int = 2,
        edge_types: Optional[Sequence[EdgeType]] = None,
        as_of: Optional[datetime] = None,
    ) -> list[GraphEdge]:
        """
        Expand outward from entrypoints (typically hybrid-search results,
        Section 3.1) up to `max_depth` hops. Shallow by design; this is not
        an exhaustive graph walk.

        UNION (not UNION ALL) dedupes the frontier, and the depth bound
        guarantees termination even on cyclic graphs. A node reachable at
        two different depths is visited twice -- acceptable at this scale,
        and the DISTINCT on the outer select keeps the result set clean.

        NOT LOAD TESTED. Revisit before production traffic (Section 12).
        """
        if not entrypoint_ids:
            return []

        # The predicate must apply inside the recursion as well as to the
        # final select. Filtering only the output would let traversal walk
        # *through* a private edge to reach nodes beyond it -- leaking the
        # graph's shape even while hiding the edge itself.
        vis_sql, vis_params = visibility_predicate(self._scope, alias="e", param_index=6)
        query = f"""
            WITH RECURSIVE frontier(node_id, node_table, depth) AS (
                SELECT eid, $2::text, 0
                FROM unnest($1::uuid[]) AS eid
              UNION
                SELECT
                    CASE WHEN e.source_id = f.node_id AND e.source_table = f.node_table
                         THEN e.target_id ELSE e.source_id END,
                    CASE WHEN e.source_id = f.node_id AND e.source_table = f.node_table
                         THEN e.target_table ELSE e.source_table END,
                    f.depth + 1
                FROM edges e
                JOIN frontier f
                  ON (e.source_id = f.node_id AND e.source_table = f.node_table)
                  OR (e.target_id = f.node_id AND e.target_table = f.node_table)
                WHERE f.depth < $3
                  AND e.t_valid <= COALESCE($5::timestamptz, now())
                  AND (e.t_invalid IS NULL OR e.t_invalid > COALESCE($5::timestamptz, now()))
                  AND ($4::text[] IS NULL OR e.edge_type::text = ANY($4::text[]))
                  AND {vis_sql}
            )
            SELECT DISTINCT {_EDGE_COLS}
            FROM edges e
            JOIN frontier f
              ON (e.source_id = f.node_id AND e.source_table = f.node_table)
              OR (e.target_id = f.node_id AND e.target_table = f.node_table)
            WHERE e.t_valid <= COALESCE($5::timestamptz, now())
              AND (e.t_invalid IS NULL OR e.t_invalid > COALESCE($5::timestamptz, now()))
              AND ($4::text[] IS NULL OR e.edge_type::text = ANY($4::text[]))
              AND {vis_sql}
        """
        types = list(edge_types) if edge_types else None
        rows = await self._pool.fetch(
            query, list(entrypoint_ids), entrypoint_table, max_depth, types, as_of,
            *vis_params,
        )
        return [_row_to_edge(r) for r in rows]

    async def blast_radius(self, task_node_id: UUID, max_depth: int = 2) -> int:
        """
        How many other TaskNodes depend on this one, transitively.

        Feeds the scorecard risk flag (Section 8.3): a change to a node
        that 30 others depend on warrants more approver scrutiny than one
        with no dependents.
        """
        edges = await self.traverse_from(
            [task_node_id], "task_nodes", max_depth=max_depth,
            edge_types=["REQUIRES", "PRODUCES", "TRIGGERED_BY"],
        )
        touched: set[UUID] = set()
        for e in edges:
            if e.source_table == "task_nodes":
                touched.add(e.source_id)
            if e.target_table == "task_nodes":
                touched.add(e.target_id)
        touched.discard(task_node_id)
        return len(touched)

    async def node_exists(self, node_id: UUID, node_table: NodeTable) -> bool:
        """Used by the groundedness check to resolve citations (Section 8.1)."""
        if node_table not in ("knowledge_nodes", "task_nodes"):
            raise ValueError(f"invalid node_table: {node_table!r}")
        vis_sql, vis_params = visibility_predicate(self._scope, param_index=2)
        row = await self._pool.fetchrow(
            f"SELECT 1 FROM {node_table} WHERE id = $1 AND t_invalid IS NULL "
            f"AND {vis_sql}",
            node_id, *vis_params,
        )
        return row is not None

    async def invalidate_edge(
        self, edge_id: UUID, at: Optional[datetime] = None,
        conn: Optional[asyncpg.Connection] = None,
    ) -> bool:
        """
        Close an edge's validity window. Never deletes -- Section 3.1's
        non-destructive update rule. Returns False if the edge was already
        invalidated (idempotent, so retries are safe).
        """
        executor = conn or self._pool
        result = await executor.execute(
            "UPDATE edges SET t_invalid = COALESCE($2, now()) "
            "WHERE id = $1 AND t_invalid IS NULL",
            edge_id, at,
        )
        return result.endswith(" 1")
