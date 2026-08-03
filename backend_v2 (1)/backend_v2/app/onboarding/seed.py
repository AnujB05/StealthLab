"""
Reusable onboarding procedure (MVP plan, Phase A item 7).

This is deliberately generic machinery, not a script for one workflow.
Onboarding a second domain must mean running this code against different
content, not writing this code again.

Input is a declarative WorkflowSpec (YAML or JSON). Nothing about any
particular industry appears below.

JSONB parameters are passed as native dicts, not pre-serialized strings
cast with ::jsonb. Pre-serializing (json.dumps + ::jsonb) corrupts the
connection's jsonb decoding for subsequent reads once a type codec is
registered (see app/db/session.py) -- confirmed against a live Postgres
instance, not theoretical. Passing native objects lets asyncpg's codec
handle encoding and keeps the connection's type resolution consistent.
"""
from __future__ import annotations

import logging
from typing import Any, Optional
from uuid import UUID

import asyncpg
from pydantic import BaseModel, Field

from app.config import settings
from app.models.ontology import EDGE_TYPES

log = logging.getLogger(__name__)


class KnowledgeSpec(BaseModel):
    key: str
    node_type: str
    name: str
    properties: dict[str, Any] = Field(default_factory=dict)


class TaskSpec(BaseModel):
    key: str
    name: str
    description: Optional[str] = None
    io_schema: dict[str, Any] = Field(default_factory=dict)
    skill_ref: Optional[str] = None
    success_criteria: dict[str, Any] = Field(default_factory=dict)
    cost_estimate: Optional[float] = None
    latency_estimate_ms: Optional[int] = None
    pert_optimistic_ms: Optional[int] = None
    pert_likely_ms: Optional[int] = None
    pert_pessimistic_ms: Optional[int] = None


class EdgeSpec(BaseModel):
    edge_type: str
    source: str
    target: str
    properties: dict[str, Any] = Field(default_factory=dict)


class WorkflowSpec(BaseModel):
    workflow_name: str
    knowledge: list[KnowledgeSpec] = Field(default_factory=list)
    tasks: list[TaskSpec] = Field(default_factory=list)
    edges: list[EdgeSpec] = Field(default_factory=list)

    def validate_spec(self) -> list[str]:
        problems: list[str] = []
        k_keys = {k.key for k in self.knowledge}
        t_keys = {t.key for t in self.tasks}

        for items, label in ((self.knowledge, "knowledge"), (self.tasks, "task")):
            seen: set[str] = set()
            for item in items:
                if item.key in seen:
                    problems.append(f"duplicate {label} key: {item.key!r}")
                seen.add(item.key)
        overlap = k_keys & t_keys
        if overlap:
            problems.append(f"keys used for both knowledge and task nodes: {sorted(overlap)}")

        all_keys = k_keys | t_keys
        for i, e in enumerate(self.edges):
            if e.edge_type not in EDGE_TYPES:
                problems.append(f"edges[{i}]: unknown edge_type {e.edge_type!r}")
            for end in ("source", "target"):
                key = getattr(e, end)
                if key not in all_keys:
                    problems.append(f"edges[{i}]: {end} {key!r} matches no declared node")
        return problems


class SeedResult(BaseModel):
    workflow_name: str
    knowledge_ids: dict[str, UUID] = Field(default_factory=dict)
    task_ids: dict[str, UUID] = Field(default_factory=dict)
    edge_count: int = 0
    embedded: int = 0
    embedding_error: Optional[str] = None


class Onboarder:
    def __init__(self, pool: asyncpg.Pool, tenant_id: Optional[UUID] = None):
        self._pool = pool
        self._tenant = tenant_id or UUID(settings.default_tenant_id)

    async def _embed_seeded(self, result: SeedResult, spec: WorkflowSpec) -> None:
        """
        Populate embeddings after the rows exist.

        Deliberately outside the seeding transaction: a Voyage outage
        should not roll back a successful graph seed. The nodes are still
        correct and usable -- they're just not semantically searchable
        until re-embedded, which `embedding_error` on the result makes
        visible rather than leaving as a silent gap.
        """
        from app.services.embeddings import Embedder, node_text, to_pgvector

        targets: list[tuple[UUID, str, str]] = []
        for k in spec.knowledge:
            targets.append((result.knowledge_ids[k.key], "knowledge_nodes", node_text(k.name)))
        for t in spec.tasks:
            targets.append(
                (result.task_ids[t.key], "task_nodes", node_text(t.name, t.description))
            )
        if not targets:
            return

        embedder = Embedder()
        try:
            vectors = await embedder.embed([text for _, _, text in targets], input_type="document")
        except Exception as exc:  # noqa: BLE001
            log.error("embedding failed during onboarding: %s", exc)
            result.embedding_error = str(exc)
            return

        async with self._pool.acquire() as conn:
            for (node_id, table, _), vector in zip(targets, vectors):
                await conn.execute(
                    f"UPDATE {table} SET embedding = $2::vector WHERE id = $1",
                    node_id, to_pgvector(vector),
                )
        result.embedded = len(targets)

    async def seed(
        self, spec: WorkflowSpec, created_by: str = "onboarding", embed: bool = True
    ) -> SeedResult:
        problems = spec.validate_spec()
        if problems:
            raise ValueError("invalid workflow spec:\n  - " + "\n  - ".join(problems))

        result = SeedResult(workflow_name=spec.workflow_name)

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                for k in spec.knowledge:
                    row = await conn.fetchrow(
                        "INSERT INTO knowledge_nodes (tenant_id, node_type, name, properties, "
                        "provenance, created_by) VALUES ($1, $2, $3, $4, "
                        "'company_ingested', $5) RETURNING id",
                        self._tenant, k.node_type, k.name, k.properties, created_by,
                    )
                    result.knowledge_ids[k.key] = row["id"]

                for t in spec.tasks:
                    row = await conn.fetchrow(
                        "INSERT INTO task_nodes (tenant_id, name, description, io_schema, "
                        "skill_ref, success_criteria, cost_estimate, latency_estimate_ms, "
                        "pert_optimistic_ms, pert_likely_ms, pert_pessimistic_ms, "
                        "provenance, created_by) "
                        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, "
                        "'company_ingested', $12) RETURNING id",
                        self._tenant, t.name, t.description, t.io_schema,
                        t.skill_ref, t.success_criteria, t.cost_estimate,
                        t.latency_estimate_ms, t.pert_optimistic_ms, t.pert_likely_ms,
                        t.pert_pessimistic_ms, created_by,
                    )
                    result.task_ids[t.key] = row["id"]

                lookup: dict[str, tuple[UUID, str]] = {
                    **{k: (v, "knowledge_nodes") for k, v in result.knowledge_ids.items()},
                    **{k: (v, "task_nodes") for k, v in result.task_ids.items()},
                }
                for e in spec.edges:
                    sid, stable = lookup[e.source]
                    tid, ttable = lookup[e.target]
                    await conn.execute(
                        "INSERT INTO edges (tenant_id, edge_type, source_id, source_table, "
                        "target_id, target_table, properties, provenance, created_by) "
                        "VALUES ($1, $2::edge_type, $3, $4, $5, $6, $7, "
                        "'company_ingested', $8)",
                        self._tenant, e.edge_type, sid, stable, tid, ttable,
                        e.properties, created_by,
                    )
                    result.edge_count += 1

        if embed:
            await self._embed_seeded(result, spec)

        log.info(
            "seeded %r: %d knowledge, %d tasks, %d edges, %d embedded",
            spec.workflow_name, len(result.knowledge_ids),
            len(result.task_ids), result.edge_count, result.embedded,
        )
        return result


def load_spec(path: str) -> WorkflowSpec:
    import json as _json
    with open(path) as fh:
        text = fh.read()
    if path.endswith((".yaml", ".yml")):
        import yaml
        return WorkflowSpec(**yaml.safe_load(text))
    return WorkflowSpec(**_json.loads(text))
