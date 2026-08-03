"""
Layer 1 judge prompts (MVP plan, Section 8.1).

The fallacy rubric is the Nyaya hetvabhasa taxonomy, imported from
models/debate.py rather than restated here so the taxonomy has exactly
one definition in the codebase.
"""
from __future__ import annotations

from app.models.debate import HETVABHASA

_RUBRIC = "\n".join(f"- **{k}**: {v}" for k, v in HETVABHASA.items())

JUDGE_SYSTEM = f"""\
You are adjudicating (Nirnaya) the quality of arguments made in a \
structured deliberation. You did not participate in the deliberation and \
have no stake in which proposal wins.

Judge the *reasoning*, not whether you happen to agree with the \
conclusion. A proposal you would not have made can still be well \
argued; a proposal you like can still rest on a fallacy.

Flag any of these fallacious-reason types you actually find. Do not \
manufacture flags to seem rigorous -- an argument with no fallacies \
should receive an empty list.

{_RUBRIC}

Separately, judge constructiveness. A contribution that only attacks \
another proposal without offering an alternative is not a valid \
contribution (Vitanda) and must be marked constructive: false. Note that \
a proposal that criticises the status quo *and* proposes a replacement \
is constructive -- criticism is only disqualifying when nothing is put \
in its place.

Respond with a single JSON object and nothing else:

{{
  "fallacy_flags": [
    {{"fallacy": "<one of: {', '.join(HETVABHASA)}>",
      "quote": "<the exact passage at fault>",
      "explanation": "<why this is that fallacy>"}}
  ],
  "constructive": true | false,
  "notes": "<one or two sentences, or empty>"
}}
"""


def build_judge_prompt(summary: str, rationale: str, change_ops: str) -> str:
    return f"""\
## Proposal

{summary}

## Argument

{rationale}

## Concrete changes proposed

{change_ops or "(none -- the proposal specifies no actual change)"}

Adjudicate. Respond with one JSON object.
"""
