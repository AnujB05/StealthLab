"""
Layer 1 evaluation (MVP plan, Section 8.1): argument quality.

Cheap enough to run on every candidate, and it gates the expensive
empirical layer that arrives at v1.1.

Deliberately split into a deterministic half and a judged half:

  - Groundedness is computed, not judged. Whether a cited node exists and
    is currently valid is a database question with a correct answer, and
    asking a model to eyeball it would introduce error where none needs to
    exist. This is also the natural place for the OWL-reasoner type-check
    from Section 4 when it lands -- same slot, stronger check.

  - Fallacy detection and constructiveness are judged, because they are
    genuinely semantic.

The judge must not be a panelist (Section 7's Nirnaya requirement): an
adjudicator scoring its own argument is not adjudication. enforce_
independence() checks this rather than leaving it to deployment
discipline.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from app.config import settings
from app.db.graph_store import GraphStore
from app.debate.panel import PanelAgent, _extract_json
from app.eval.prompts import JUDGE_SYSTEM, build_judge_prompt
from app.models.debate import HETVABHASA, Candidate, FallacyFlag, Layer1Result

log = logging.getLogger(__name__)


class JudgeNotIndependent(Exception):
    pass


def enforce_independence(judge: PanelAgent, panel: list[PanelAgent]) -> None:
    """Section 7/8.1: the adjudicator must not have argued the case."""
    if judge.agent_id in {a.agent_id for a in panel}:
        raise JudgeNotIndependent(
            f"judge {judge.agent_id!r} was also a panelist; an adjudicator "
            "cannot score its own argument"
        )
    if judge.family in {a.family for a in panel}:
        raise JudgeNotIndependent(
            f"judge family {judge.family!r} is represented on the panel; "
            "use a model family that did not participate"
        )


class Layer1Evaluator:
    def __init__(self, judge: PanelAgent, graph: Optional[GraphStore] = None, on_call=None):
        self.judge = judge
        self.graph = graph
        self.on_call = on_call  # cost-recording hook, see debate/engine.py

    async def _groundedness(self, candidate: Candidate, cited) -> tuple[float, list[str]]:
        """
        Fraction of citations that resolve to a live node.

        Returns 0.0 for an uncited proposal: a claim about this company's
        workflow with no anchor in this company's graph is exactly what
        this check exists to catch. Scored, not rejected -- an agent can
        reason correctly from something not yet in the graph, and that
        case should surface on the scorecard rather than disappear.
        """
        if not cited:
            return 0.0, []
        if self.graph is None:
            # No graph wired (offline tests): report unknown rather than
            # inventing a passing score.
            return 0.0, ["graph unavailable -- citations unverified"]

        unresolved: list[str] = []
        resolved = 0
        for cite in cited:
            try:
                exists = await self.graph.node_exists(cite.node_id, cite.node_table)
            except Exception as exc:  # noqa: BLE001
                log.warning("citation lookup failed for %s: %s", cite.node_id, exc)
                exists = False
            if exists:
                resolved += 1
            else:
                unresolved.append(f"{cite.node_table}:{cite.node_id}")
        return resolved / len(cited), unresolved

    async def evaluate(self, candidate: Candidate, cited=None) -> Layer1Result:
        cited = cited or []
        structural = candidate.change_set.validate_ops()
        score, unresolved = await self._groundedness(candidate, cited)

        flags: list[FallacyFlag] = []
        constructive = True
        notes = ""

        ops_text = json.dumps(
            [op.model_dump(mode="json") for op in candidate.change_set.ops], indent=2
        )
        try:
            prompt = build_judge_prompt(candidate.summary, candidate.rationale, ops_text)
            raw = await self.judge.respond(JUDGE_SYSTEM, prompt)
            if self.on_call:
                await self.on_call(self.judge, JUDGE_SYSTEM + prompt, raw)
            payload = _extract_json(raw)
            for item in payload.get("fallacy_flags", []) or []:
                name = str(item.get("fallacy", "")).lower().strip()
                if name not in HETVABHASA:
                    # A judge inventing fallacy categories outside the
                    # rubric is a prompt-adherence failure, not a finding.
                    log.warning("judge returned unknown fallacy %r; discarding", name)
                    continue
                flags.append(FallacyFlag(
                    fallacy=name,
                    quote=str(item.get("quote", ""))[:500],
                    explanation=str(item.get("explanation", ""))[:1000],
                ))
            constructive = bool(payload.get("constructive", True))
            notes = str(payload.get("notes", ""))[:1000]
        except Exception as exc:  # noqa: BLE001
            # A judge failure must not silently pass a candidate. Record it
            # and let the gate below fail closed.
            log.error("judge failed on candidate %s: %s", candidate.id, exc)
            notes = f"judge unavailable: {exc}"
            structural = structural + ["layer 1 judgment incomplete"]

        passed = (
            constructive
            and not flags
            and not structural
            and score >= settings.groundedness_threshold
        )

        return Layer1Result(
            candidate_id=candidate.id,
            fallacy_flags=flags,
            constructive=constructive,
            groundedness_score=round(score, 3),
            unresolved_cites=unresolved,
            structural_problems=structural,
            passed=passed,
            notes=notes,
        )
