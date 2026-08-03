"""
Subgraph endpoint for visualization (task decomposition add-on).

Reuses GraphStore.traverse_from directly -- no new graph logic, just a
response shape a frontend node-graph library (React Flow, etc.) can
consume without transformation.
"""
from __future__ import annotations

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from app.api.deps import get_scope
from app.db.graph_store import GraphStore
from app.services.access import AccessScope, visibility_predicate

router = APIRouter(prefix="/v1/graph", tags=["graph"])


async def get_pool(request: Request):
    return request.app.state.pool


class GraphNode(BaseModel):
    id: UUID
    table: Literal["knowledge_nodes", "task_nodes"]
    label: str


class GraphEdgeOut(BaseModel):
    id: UUID
    source: UUID
    target: UUID
    label: str


class SubgraphResponse(BaseModel):
    center: UUID
    nodes: list[GraphNode]
    edges: list[GraphEdgeOut]


@router.get("/{node_id}", response_model=SubgraphResponse)
async def get_subgraph(
    node_id: UUID,
    depth: int = Query(default=2, ge=1, le=4),
    pool=Depends(get_pool),
    scope: AccessScope = Depends(get_scope),
) -> SubgraphResponse:
    graph = GraphStore(pool, scope=scope)

    # Figure out which table the center node lives in -- callers shouldn't
    # need to know this ahead of time.
    table = None
    for candidate in ("task_nodes", "knowledge_nodes"):
        if await graph.node_exists(node_id, candidate):
            table = candidate
            break
    if table is None:
        raise HTTPException(404, "node not found")

    edges = await graph.traverse_from([node_id], table, max_depth=depth)

    node_ids: dict[UUID, str] = {node_id: table}
    for e in edges:
        node_ids[e.source_id] = e.source_table
        node_ids[e.target_id] = e.target_table

    nodes: list[GraphNode] = []
    for nid, ntable in node_ids.items():
        vis_sql, vis_params = visibility_predicate(scope, param_index=2)
        row = await pool.fetchrow(
            f"SELECT name FROM {ntable} WHERE id = $1 AND {vis_sql}", nid, *vis_params
        )
        # A node the viewer can't see is omitted rather than labelled
        # "?" -- a placeholder would still reveal that something exists.
        if row:
            nodes.append(GraphNode(id=nid, table=ntable, label=row["name"]))

    return SubgraphResponse(
        center=node_id,
        nodes=nodes,
        edges=[
            GraphEdgeOut(id=e.id, source=e.source_id, target=e.target_id,
                        label=e.custom_edge_type or e.edge_type)
            for e in edges
        ],
    )
