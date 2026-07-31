"""
Markdown diff rendering (MVP plan, Section 10 delivery, Section 8.3 display).

Rendered from the same ChangeSet object that KnowledgeUpdater applies.
That identity is the point: the approver reads a diff generated from the
exact operations that will execute, so there is no gap between what was
approved and what gets written.

Serves both purposes deliberately -- the approval-page view and the
human-applied export are the same artifact at different moments, and
letting them drift would mean approving one thing and exporting another.
"""
from __future__ import annotations

import json
from typing import Any, Mapping, Optional

from app.models.change import ChangeSet, CreateEdgeOp, InvalidateEdgeOp, UpdateTaskNodeOp
from app.models.debate import Scorecard

NodeNames = Mapping[str, str]  # str(uuid) -> human-readable name


def _name(names: Optional[NodeNames], node_id: Any) -> str:
    key = str(node_id)
    if names and key in names:
        return f"**{names[key]}** (`{key[:8]}`)"
    return f"`{key}`"


def render_change_set(cs: ChangeSet, names: Optional[NodeNames] = None) -> str:
    if not cs.ops:
        return "_No changes specified._"

    out: list[str] = []
    for op in cs.ops:
        if isinstance(op, UpdateTaskNodeOp):
            out.append(f"### Modify task: {_name(names, op.task_node_id)}\n")
            out.append(f"_Reason:_ {op.reason}\n")
            for field, value in op.changes.items():
                rendered = (
                    json.dumps(value, indent=2)
                    if isinstance(value, (dict, list))
                    else str(value)
                )
                out.append(f"- `{field}` →\n\n```\n{rendered}\n```\n")
        elif isinstance(op, InvalidateEdgeOp):
            out.append(f"### Remove relationship `{str(op.edge_id)[:8]}`\n")
            out.append(f"_Reason:_ {op.reason}\n")
            out.append(
                "_The relationship is closed as of the approval time, not deleted; "
                "history remains queryable._\n"
            )
        elif isinstance(op, CreateEdgeOp):
            label = op.custom_edge_type or op.edge_type
            out.append(
                f"### Add relationship: {_name(names, op.source_id)} "
                f"—[{label}]→ {_name(names, op.target_id)}\n"
            )
            if op.properties:
                out.append(f"```json\n{json.dumps(op.properties, indent=2)}\n```\n")
    return "\n".join(out)


def render_scorecard(sc: Scorecard, names: Optional[NodeNames] = None,
                     change_set: Optional[ChangeSet] = None) -> str:
    l1 = sc.layer1
    lines = [
        f"## Candidate: {sc.summary}",
        "",
        f"- **Proposed by:** {', '.join(sc.proposers) or 'unknown'}",
        f"- **Layer 1:** {'PASSED' if l1.passed else 'FAILED'}",
        f"- **Groundedness:** {l1.groundedness_score:.2f}",
        f"- **Constructive:** {'yes' if l1.constructive else 'no (refutation without alternative)'}",
        f"- **Blast radius:** {sc.blast_radius} dependent task(s)",
        f"- **Reversible:** {'yes' if sc.reversible else 'no'}",
    ]

    if l1.fallacy_flags:
        lines += ["", "### Reasoning flags", ""]
        for f in l1.fallacy_flags:
            lines.append(f"- **{f.fallacy}** — {f.explanation}")
            if f.quote:
                lines.append(f"  > {f.quote}")

    if l1.structural_problems:
        lines += ["", "### Structural problems", ""]
        lines += [f"- {p}" for p in l1.structural_problems]

    if l1.unresolved_cites:
        lines += ["", "### Unresolved citations", ""]
        lines += [f"- `{c}`" for c in l1.unresolved_cites]

    # Layer 2 is absent in v0. Say so explicitly rather than leaving a
    # silent gap the approver might read as "tested and fine".
    lines += ["", "### Empirical evidence", ""]
    if sc.layer2_tier is None:
        lines.append(
            "_Not yet available. No replay or shadow testing has been run on this "
            "candidate — Layer 1 assesses the argument only, not the outcome._"
        )
    else:
        lines.append(f"Tier {sc.layer2_tier}: `{json.dumps(sc.layer2_metrics)}`")

    if change_set is not None:
        lines += ["", "### Proposed changes", "", render_change_set(change_set, names)]

    lines += [
        "", "### Recommendation", "",
        sc.recommendation or "_none_",
        "", "_Advisory only. This is not an approval and carries no authority._",
    ]
    return "\n".join(lines)


def render_export(
    scorecard: Scorecard, change_set: ChangeSet, approver_id: str,
    approved_at: str, names: Optional[NodeNames] = None,
) -> str:
    """The human-applied export (Section 10, delivery option 1)."""
    return "\n".join([
        f"# Approved workflow change: {scorecard.summary}",
        "",
        f"- **Approved by:** {approver_id}",
        f"- **Approved at:** {approved_at}",
        f"- **Debate:** `{scorecard.debate_id}`",
        f"- **Candidate:** `{scorecard.candidate_id}`",
        "",
        "## Changes to apply",
        "",
        render_change_set(change_set, names),
        "",
        "---",
        "",
        "_Generated from the same change set recorded against this approval._",
    ])
