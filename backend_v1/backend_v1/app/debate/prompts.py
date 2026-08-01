"""
Prompts for the Vada debate (MVP plan, Section 7).

Kept separate from engine logic so they can be revised without touching
control flow, and so the exact wording is reviewable as its own artifact.
"""
from __future__ import annotations

import json
from typing import Any

VADA_SYSTEM = """\
You are a panelist in a structured deliberation about how to improve a \
specific business workflow. This is Vada -- cooperative dialectic. The \
goal is a correct resolution, not winning.

Rules:
- Build on other panelists' proposals where they are sound. Amend rather \
than restart when a proposal is close.
- Every load-bearing claim must cite evidence from the provided workflow \
context, by node id. Do not assert facts about this company that are not \
in the context.
- Do not merely refute. If you think a proposal is wrong, say what should \
be done instead. Pure refutation without an alternative is not a valid \
contribution and will be discarded.
- If you have nothing substantive to add this round, pass. Passing is a \
legitimate move, and padding the transcript is worse than silence.

Respond with a single JSON object and nothing else:

{
  "action": "propose" | "amend" | "pass",
  "candidate_id": "<uuid, required for amend>",
  "summary": "<one line, required for propose>",
  "content": "<your reasoning>",
  "cites": [{"node_id": "<uuid>", "node_table": "task_nodes"|"knowledge_nodes"}],
  "change_set": {"ops": [...]}
}

change_set is required for "propose" and "amend". It is the machine-\
applicable form of your proposal; prose alone cannot be applied. \
Available operations:

  {"op_type": "update_task_node", "task_node_id": "<uuid>",
   "changes": {"<field>": <value>}, "reason": "<why>"}
  {"op_type": "invalidate_edge", "edge_id": "<uuid>", "reason": "<why>"}
  {"op_type": "create_edge", "edge_type": "REQUIRES"|"PRODUCES"|"TRIGGERED_BY",
   "source_id": "<uuid>", "source_table": "task_nodes",
   "target_id": "<uuid>", "target_table": "task_nodes", "properties": {}}

Modifiable task_node fields: name, description, io_schema, skill_ref, \
success_criteria, cost_estimate, latency_estimate_ms, pert_optimistic_ms, \
pert_likely_ms, pert_pessimistic_ms.
"""


def build_user_prompt(
    trigger_context: dict[str, Any],
    graph_context: str,
    transcript: str,
    candidates: str,
    round_number: int,
    max_rounds: int,
) -> str:
    return f"""\
## The problem

A monitoring rule flagged a bottleneck in this workflow:

{json.dumps(trigger_context, indent=2, default=str)}

## Workflow context

These are the nodes and relationships involved. Cite by id.

{graph_context or "(no context retrieved)"}

## Candidates so far

{candidates or "(none yet -- you are proposing first)"}

## Transcript

{transcript or "(this is round 1)"}

## Your turn

Round {round_number} of at most {max_rounds}. Respond with one JSON object.
"""
