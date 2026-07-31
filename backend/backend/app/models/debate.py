"""
Debate and evaluation models (MVP plan, Sections 7 and 8).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from app.models.change import ChangeSet

DebateState = Literal[
    "OPEN", "IN_DEBATE", "PENDING_EVAL", "PENDING_APPROVAL", "APPROVED", "REJECTED"
]
TurnAction = Literal["propose", "amend", "pass"]
SpeakerKind = Literal["agent", "human"]

# The five hetvabhasa (fallacious-reason) types, Section 8.1. Descriptions
# are the actual text handed to the judge model -- keeping them here rather
# than inline in the prompt means the taxonomy is versioned with the code.
HETVABHASA: dict[str, str] = {
    "asiddha": (
        "Unestablished: the reason rests on a premise that is not itself "
        "established as true (e.g. cites a fact not in evidence)."
    ),
    "viruddha": (
        "Contradictory: the reason, if accepted, actually supports the "
        "opposite of the conclusion drawn."
    ),
    "anaikantika": (
        "Inconclusive/deviating: the reason does not reliably track the "
        "conclusion -- it holds in cases where the conclusion is false."
    ),
    "kalatita": (
        "Mistimed: reasoning valid at some other time but not now, e.g. "
        "relying on a condition that has since been invalidated."
    ),
    "prakaranasama": (
        "Question-begging: the reason presupposes the very point at issue."
    ),
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Citation(BaseModel):
    """A claim's evidentiary anchor in the instance graph."""

    node_id: UUID
    node_table: Literal["knowledge_nodes", "task_nodes"]


class DebateTurn(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    debate_id: UUID
    round_number: int
    speaker_id: str
    speaker_kind: SpeakerKind = "agent"
    speaker_role: Optional[str] = None  # human panelists only; unenforced placeholder, Section 12
    # Recorded, not merely intended: heterogeneity is enforced by checking
    # this field across a panel, not by trusting the roster config.
    model_used: Optional[str] = None
    action: TurnAction
    candidate_id: Optional[UUID] = None
    content: str
    cites: list[Citation] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_now)


class Candidate(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    debate_id: UUID
    summary: str
    rationale: str
    change_set: ChangeSet = Field(default_factory=ChangeSet)
    supporters: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    def add_supporter(self, speaker_id: str) -> None:
        if speaker_id not in self.supporters:
            self.supporters.append(speaker_id)
            self.updated_at = _now()


class FallacyFlag(BaseModel):
    fallacy: str          # key from HETVABHASA
    quote: str            # the specific passage judged fallacious
    explanation: str


class Layer1Result(BaseModel):
    """Argument-quality evaluation (Section 8.1). Cheap, runs on everything."""

    candidate_id: UUID
    fallacy_flags: list[FallacyFlag] = Field(default_factory=list)
    constructive: bool = True
    # Fraction of load-bearing claims anchored to a resolvable, currently
    # valid graph node. Computed deterministically, not by the judge.
    groundedness_score: float = 0.0
    unresolved_cites: list[str] = Field(default_factory=list)
    structural_problems: list[str] = Field(default_factory=list)
    passed: bool = False
    notes: str = ""


class Scorecard(BaseModel):
    """
    Per-candidate output (Section 8.3). Deliberately not a winner:
    the approver sees every surviving candidate side by side.
    """

    id: UUID = Field(default_factory=uuid4)
    debate_id: UUID
    candidate_id: UUID
    summary: str
    proposers: list[str] = Field(default_factory=list)
    layer1: Layer1Result

    # Layer 2 (Section 8.2) is out of scope for v0. Fields exist so the
    # schema and API shape don't change when it lands at v1.1.
    layer2_tier: Optional[int] = None
    layer2_metrics: Optional[dict] = None
    value_delivered: Optional[float] = None  # metering for pricing (Section 13)

    blast_radius: int = 0
    reversible: bool = True
    recommendation: str = ""  # advisory only, never binding
    created_at: datetime = Field(default_factory=_now)


class DebateResult(BaseModel):
    debate_id: UUID
    trigger_id: UUID
    rounds_used: int
    termination_reason: Literal["converged", "round_cap", "no_candidates"]
    turns: list[DebateTurn] = Field(default_factory=list)
    candidates: list[Candidate] = Field(default_factory=list)
    agent_failures: list[str] = Field(default_factory=list)

    def eligible_candidates(self, min_supporters: int) -> list[Candidate]:
        """Section 7: candidates with support from >= N panelists go to eval."""
        return [c for c in self.candidates if len(c.supporters) >= min_supporters]
