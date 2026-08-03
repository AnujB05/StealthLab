"""
Trace ingestion models (MVP plan, Section 6).

Field names deliberately mirror OpenTelemetry GenAI semantic conventions
(`gen_ai.*` span kinds) so the mapping layer stays thin. Per Section 10,
that spec is pre-1.0 -- the convention strings live in
OTEL_ACTION_MAP below and nowhere else, so a spec bump touches one dict.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field

ActionType = Literal["invoke_agent", "execute_tool", "human_review"]
Outcome = Literal["success", "failure", "needs_rework"]

# The one place OTel convention strings appear. Pin the spec version you
# tested against and update here when it moves.
OTEL_SPEC_VERSION = "1.30.0-experimental"
OTEL_ACTION_MAP: dict[str, ActionType] = {
    "gen_ai.invoke_agent": "invoke_agent",
    "gen_ai.execute_tool": "execute_tool",
    "gen_ai.chat": "invoke_agent",
    "human.review": "human_review",
}


class TraceRecord(BaseModel):
    trace_id: str
    timestamp: datetime
    task_node_id: UUID
    outcome: Outcome
    actor_id: Optional[str] = None
    action_type: ActionType = "invoke_agent"
    cost: Optional[float] = Field(default=None, ge=0)
    latency_ms: Optional[int] = Field(default=None, ge=0)
    parent_trace_id: Optional[str] = None


class TraceBatch(BaseModel):
    records: list[TraceRecord]


class RejectedRecord(BaseModel):
    index: int
    trace_id: Optional[str] = None
    error: str


class IngestResult(BaseModel):
    """
    Per-record outcomes. Section 6 requires malformed records be rejected
    individually rather than failing the whole batch, so a single bad row
    from a customer's exporter can't stall their entire pipeline.
    """

    accepted: int
    rejected: list[RejectedRecord] = Field(default_factory=list)
    duplicates: int = 0
