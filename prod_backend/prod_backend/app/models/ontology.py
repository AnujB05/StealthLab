"""
Pydantic models mirroring db/01_ontology.sql (MVP plan, Section 3.1).

The DB stores bi-temporal fields as four flat columns; these models group
them into a BiTemporal sub-object for readability. That mismatch is
bridged explicitly by from_row()/to_row() rather than being left implicit
-- if you add a column, add it in both places.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Mapping, Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

ProvenanceSource = Literal["company_ingested", "company_debate", "prior_library"]
EdgeType = Literal[
    "REQUIRES", "PRODUCES", "TRIGGERED_BY", "SUPERSEDES",
    "VALIDATED_BY", "OWNS", "RESPONSIBLE_FOR",
]
NodeTable = Literal["knowledge_nodes", "task_nodes"]
LinkTable = Literal["knowledge_nodes", "task_nodes", "edges"]

EDGE_TYPES: tuple[str, ...] = (
    "REQUIRES", "PRODUCES", "TRIGGERED_BY", "SUPERSEDES",
    "VALIDATED_BY", "OWNS", "RESPONSIBLE_FOR",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class BiTemporal(BaseModel):
    """t_valid/t_invalid = world truth; t_created/t_expired = system truth."""

    t_valid: datetime = Field(default_factory=_now)
    t_invalid: Optional[datetime] = None
    t_created: datetime = Field(default_factory=_now)
    t_expired: Optional[datetime] = None

    @property
    def is_current(self) -> bool:
        return self.t_invalid is None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "BiTemporal":
        return cls(
            t_valid=row["t_valid"],
            t_invalid=row.get("t_invalid"),
            t_created=row["t_created"],
            t_expired=row.get("t_expired"),
        )

    def to_row(self) -> dict[str, Any]:
        return {
            "t_valid": self.t_valid,
            "t_invalid": self.t_invalid,
            "t_created": self.t_created,
            "t_expired": self.t_expired,
        }


class KnowledgeNode(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    tenant_id: UUID
    node_type: str  # entity | policy | metric | data_object | person | ...
    name: str
    properties: dict = Field(default_factory=dict)
    embedding: Optional[list[float]] = None
    provenance: ProvenanceSource = "company_ingested"
    temporal: BiTemporal = Field(default_factory=BiTemporal)
    created_by: Optional[str] = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "KnowledgeNode":
        return cls(
            id=row["id"],
            tenant_id=row["tenant_id"],
            node_type=row["node_type"],
            name=row["name"],
            properties=row.get("properties") or {},
            embedding=row.get("embedding"),
            provenance=row["provenance"],
            temporal=BiTemporal.from_row(row),
            created_by=row.get("created_by"),
        )


class TaskNode(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    tenant_id: UUID
    name: str
    description: Optional[str] = None
    io_schema: dict = Field(default_factory=dict)  # a JSON Schema document
    skill_ref: Optional[str] = None
    success_criteria: dict = Field(default_factory=dict)
    cost_estimate: Optional[float] = None
    latency_estimate_ms: Optional[int] = None
    pert_optimistic_ms: Optional[int] = None
    pert_likely_ms: Optional[int] = None
    pert_pessimistic_ms: Optional[int] = None
    embedding: Optional[list[float]] = None
    provenance: ProvenanceSource = "company_ingested"
    temporal: BiTemporal = Field(default_factory=BiTemporal)
    created_by: Optional[str] = None

    @property
    def pert_expected_ms(self) -> Optional[float]:
        """PERT expected duration: (a + 4m + b) / 6 (Section 4 apparatus).

        Used as a duration prior at cold start, before real timing data
        accumulates. Returns None unless all three estimates are present.
        """
        a, m, b = self.pert_optimistic_ms, self.pert_likely_ms, self.pert_pessimistic_ms
        if a is None or m is None or b is None:
            return None
        return (a + 4 * m + b) / 6

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "TaskNode":
        return cls(
            id=row["id"],
            tenant_id=row["tenant_id"],
            name=row["name"],
            description=row.get("description"),
            io_schema=row.get("io_schema") or {},
            skill_ref=row.get("skill_ref"),
            success_criteria=row.get("success_criteria") or {},
            cost_estimate=float(row["cost_estimate"]) if row.get("cost_estimate") is not None else None,
            latency_estimate_ms=row.get("latency_estimate_ms"),
            pert_optimistic_ms=row.get("pert_optimistic_ms"),
            pert_likely_ms=row.get("pert_likely_ms"),
            pert_pessimistic_ms=row.get("pert_pessimistic_ms"),
            embedding=row.get("embedding"),
            provenance=row["provenance"],
            temporal=BiTemporal.from_row(row),
            created_by=row.get("created_by"),
        )


class Edge(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    tenant_id: UUID
    edge_type: EdgeType
    custom_edge_type: Optional[str] = None  # tenant-specific extensible slot
    source_id: UUID
    source_table: NodeTable
    target_id: UUID
    target_table: NodeTable
    properties: dict = Field(default_factory=dict)
    provenance: ProvenanceSource = "company_ingested"
    temporal: BiTemporal = Field(default_factory=BiTemporal)
    created_by: Optional[str] = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Edge":
        return cls(
            id=row["id"],
            tenant_id=row["tenant_id"],
            edge_type=row["edge_type"],
            custom_edge_type=row.get("custom_edge_type"),
            source_id=row["source_id"],
            source_table=row["source_table"],
            target_id=row["target_id"],
            target_table=row["target_table"],
            properties=row.get("properties") or {},
            provenance=row["provenance"],
            temporal=BiTemporal.from_row(row),
            created_by=row.get("created_by"),
        )


class Episode(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    tenant_id: UUID
    episode_type: Literal["document", "trace", "debate_transcript"]
    content: Optional[str] = None
    content_ref: Optional[str] = None  # Supabase Storage path for large payloads
    timestamp: datetime = Field(default_factory=_now)
    metadata: dict = Field(default_factory=dict)
