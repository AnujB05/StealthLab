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


class CreateTaskNodeOp(BaseModel):
    """
    Create a new TaskNode.

    Absent from V0/V1 entirely, and that absence was correct there: the
    graph was authored by a human offline and the debate panel could only
    refine what already existed. V2's generative decomposition needs to
    introduce tasks that didn't exist a moment ago, which is a
    categorically different operation.

    Deliberately has no counterpart for *modifying* or *deleting* nodes
    from generated input -- see `GENERATIVE_OP_TYPES` below.
    """

    op_type: Literal["create_task_node"] = "create_task_node"
    name: str = Field(min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=2000)
    io_schema: dict[str, Any] = Field(default_factory=dict)
    skill_ref: Optional[str] = Field(default=None, max_length=200)
    success_criteria: dict[str, Any] = Field(default_factory=dict)
    # Local handle used to wire edges within one change set, before the
    # database has assigned real ids.
    ref: str = Field(min_length=1, max_length=64)


class CreateKnowledgeNodeOp(BaseModel):
    op_type: Literal["create_knowledge_node"] = "create_knowledge_node"
    node_type: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    properties: dict[str, Any] = Field(default_factory=dict)
    ref: str = Field(min_length=1, max_length=64)


class InvalidateEdgeOp(BaseModel):
    """Close an existing edge's validity window. Never deletes."""

    op_type: Literal["invalidate_edge"] = "invalidate_edge"
    edge_id: UUID
    reason: str


class CreateEdgeOp(BaseModel):
    """
    Create an edge.

    Endpoints are given either as a real `UUID` (an existing node) or as
    a local `ref` string matching a create-op in the same change set.
    Generated decompositions use refs exclusively, because the nodes they
    connect don't have ids until the set is applied.
    """

    op_type: Literal["create_edge"] = "create_edge"
    edge_type: EdgeType
    custom_edge_type: Optional[str] = None
    source_id: Optional[UUID] = None
    source_ref: Optional[str] = None
    source_table: NodeTable = "task_nodes"
    target_id: Optional[UUID] = None
    target_ref: Optional[str] = None
    target_table: NodeTable = "task_nodes"
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
    Union[
        InvalidateEdgeOp,
        CreateEdgeOp,
        UpdateTaskNodeOp,
        CreateTaskNodeOp,
        CreateKnowledgeNodeOp,
    ],
    Field(discriminator="op_type"),
]

# The capability boundary for LLM-generated change sets (V2 Tab 1).
#
# This is the primary defence against prompt injection, and it is
# deliberately structural rather than textual. Input scanning and careful
# prompting both reduce the chance a model is hijacked; neither can
# guarantee it. What *can* be guaranteed is that a hijacked model has
# nothing dangerous to reach for: generated ops may only create new
# nodes and connect them to each other. They cannot modify, invalidate,
# or attach to anything that already exists.
#
# So the worst case for a fully successful injection is a junk subgraph
# sitting in quarantine awaiting human approval -- not a corrupted or
# deleted production workflow.
GENERATIVE_OP_TYPES: frozenset[str] = frozenset({
    "create_task_node",
    "create_knowledge_node",
    "create_edge",
})

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
                if not (op.source_id or op.source_ref):
                    problems.append(f"op[{i}]: edge has no source")
                if not (op.target_id or op.target_ref):
                    problems.append(f"op[{i}]: edge has no target")
                same_id = op.source_id and op.source_id == op.target_id
                same_ref = op.source_ref and op.source_ref == op.target_ref
                if same_id or same_ref:
                    problems.append(f"op[{i}]: self-referential edge")

        # Local refs must resolve within this change set, or the edge
        # would dangle at apply time.
        declared = {
            op.ref for op in self.ops
            if isinstance(op, (CreateTaskNodeOp, CreateKnowledgeNodeOp))
        }
        duplicates = [
            op.ref for op in self.ops
            if isinstance(op, (CreateTaskNodeOp, CreateKnowledgeNodeOp))
        ]
        if len(duplicates) != len(declared):
            problems.append("duplicate node refs in change set")

        for i, op in enumerate(self.ops):
            if isinstance(op, CreateEdgeOp):
                for end, ref in (("source", op.source_ref), ("target", op.target_ref)):
                    if ref and ref not in declared:
                        problems.append(
                            f"op[{i}]: {end}_ref {ref!r} matches no node in this change set"
                        )
        return problems

    def validate_generative(self) -> list[str]:
        """
        Stricter validation for LLM-generated change sets.

        Enforces the capability boundary: only creation ops are allowed.
        This is what makes a hijacked model harmless rather than
        dangerous, so it is checked structurally here rather than
        depended upon from prompt wording.
        """
        problems = self.validate_ops()
        for i, op in enumerate(self.ops):
            if op.op_type not in GENERATIVE_OP_TYPES:
                problems.append(
                    f"op[{i}]: {op.op_type!r} is not permitted from generated input "
                    f"(only {sorted(GENERATIVE_OP_TYPES)} may be generated)"
                )
            if isinstance(op, CreateEdgeOp) and (op.source_id or op.target_id):
                problems.append(
                    f"op[{i}]: generated edges may not attach to existing nodes by id; "
                    "use refs to nodes created in the same change set"
                )
        return problems

    @property
    def touched_task_nodes(self) -> set[UUID]:
        return {op.task_node_id for op in self.ops if isinstance(op, UpdateTaskNodeOp)}
