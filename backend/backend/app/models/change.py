"""
ChangeSet: the machine-applicable representation of what a debate
candidate actually proposes to change.

NOT IN THE ORIGINAL PLAN -- added during implementation. The plan had
candidates carrying prose rationale and the approval step "writing the
update", with nothing in between. Prose can't be applied to a graph, so
without this the approval step would either need a human to hand-translate
every approved change (defeating the automation claim) or an LLM to
interpret prose into mutations at write time (unreviewable -- the approver
would be signing off on text while something else got written).

Making the change explicit and structured means the approver sees exactly
the operations that will execute, and the diff shown to them (see
export/markdown_diff.py) is generated from the same object that gets
applied. Nothing is interpreted between approval and write.

Every operation is expressed so it can be reversed, which is what makes
the `reversible` flag on a scorecard meaningful rather than aspirational.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal, Optional, Union
from uuid import UUID

from pydantic import BaseModel, Field

from app.models.ontology import EdgeType, NodeTable


class InvalidateEdgeOp(BaseModel):
    """Close an existing edge's validity window. Never deletes."""

    op_type: Literal["invalidate_edge"] = "invalidate_edge"
    edge_id: UUID
    reason: str


class CreateEdgeOp(BaseModel):
    op_type: Literal["create_edge"] = "create_edge"
    edge_type: EdgeType
    custom_edge_type: Optional[str] = None
    source_id: UUID
    source_table: NodeTable
    target_id: UUID
    target_table: NodeTable
    properties: dict[str, Any] = Field(default_factory=dict)


class UpdateTaskNodeOp(BaseModel):
    """
    Supersede a TaskNode: close the old row's window and insert a new
    version carrying `changes` merged over it. Implemented as
    invalidate-and-append like everything else, not an UPDATE.
    """

    op_type: Literal["update_task_node"] = "update_task_node"
    task_node_id: UUID
    changes: dict[str, Any]
    reason: str


ChangeOp = Annotated[
    Union[InvalidateEdgeOp, CreateEdgeOp, UpdateTaskNodeOp],
    Field(discriminator="op_type"),
]

# Fields on task_nodes that a debate candidate is allowed to modify.
# Deliberately excludes id, tenant_id, provenance, and all four temporal
# columns: those are managed by the update machinery, not by proposals.
MUTABLE_TASK_FIELDS: frozenset[str] = frozenset({
    "name", "description", "io_schema", "skill_ref", "success_criteria",
    "cost_estimate", "latency_estimate_ms",
    "pert_optimistic_ms", "pert_likely_ms", "pert_pessimistic_ms",
})


class ChangeSet(BaseModel):
    ops: list[ChangeOp] = Field(default_factory=list)

    def validate_ops(self) -> list[str]:
        """
        Structural validation, returning human-readable problems.

        Deliberately returns a list rather than raising: a malformed
        change set is a reason to fail the candidate on its scorecard,
        not to crash the debate.
        """
        problems: list[str] = []
        if not self.ops:
            problems.append("change set is empty -- candidate proposes no actual change")

        for i, op in enumerate(self.ops):
            if isinstance(op, UpdateTaskNodeOp):
                if not op.changes:
                    problems.append(f"op[{i}]: update_task_node with no changes")
                illegal = set(op.changes) - MUTABLE_TASK_FIELDS
                if illegal:
                    problems.append(
                        f"op[{i}]: cannot modify protected field(s): {sorted(illegal)}"
                    )
            elif isinstance(op, CreateEdgeOp):
                if op.source_id == op.target_id and op.source_table == op.target_table:
                    problems.append(f"op[{i}]: self-referential edge")
        return problems

    @property
    def touched_task_nodes(self) -> set[UUID]:
        return {op.task_node_id for op in self.ops if isinstance(op, UpdateTaskNodeOp)}
