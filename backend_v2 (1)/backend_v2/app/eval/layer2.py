"""
Layer 2 empirical evaluation (MVP plan Section 8.2).

Three evidence tiers, deliberately ranked and always labelled — a
scorecard must never present simulated evidence as if it were
observed:

  Tier 1  shadow deployment      strongest   NOT IMPLEMENTED (blocked on
                                             the execution-ownership
                                             decision + a customer-side
                                             API contract)
  Tier 2  off-policy evaluation  moderate    NOT IMPLEMENTED (blocked on
                                             a data gap: `traces` records
                                             task executions, not policy
                                             decisions, so there is no
                                             logged action-variation to
                                             reweight. Fixing this needs
                                             variant tracking added to
                                             traces and real production
                                             variation to accumulate.)
  Tier 3  simulated replay       weakest     implemented here

Tier 3's honest description: an LLM is shown a historical execution and
a proposed change, and asked what would plausibly have happened. That is
a *model's opinion about a counterfactual*, not a measurement. It is
useful for catching obviously-bad candidates cheaply, and close to
worthless as positive evidence that something works. Everything below
is built to keep that distinction visible rather than let a number
launder it.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence
from uuid import UUID

import asyncpg

from app.debate.panel import PanelAgent, _extract_json
from app.eval.statistics import (
    ComparisonResult,
    SequentialPlan,
    Verdict,
    benjamini_hochberg,
    required_sample_size,
    welch_comparison,
)
from app.models.change import ChangeSet

log = logging.getLogger(__name__)

TIER_SHADOW = 1
TIER_OFF_POLICY = 2
TIER_SIMULATED = 3

TIER_LABELS = {
    TIER_SHADOW: "shadow deployment (observed)",
    TIER_OFF_POLICY: "off-policy estimate (observed logs)",
    TIER_SIMULATED: "simulated replay (model opinion, not measurement)",
}

# Metrics evaluated, and which direction counts as improvement.
METRIC_DIRECTIONS = {
    "success_rate": True,      # higher is better
    "latency_ms": False,
    "cost": False,
    "rework_rate": False,
}

SIMULATION_SYSTEM = """\
You are estimating how a proposed change to a business workflow would \
have affected a specific past execution of that workflow.

You are not being asked whether the change is a good idea. You are being \
asked to predict a concrete outcome, as neutrally as you can.

Be conservative. If the change plausibly would not have affected this \
particular execution, say so — predicting improvement everywhere is a \
failure mode, not helpfulness. Many changes affect only a subset of \
cases.

Respond with a single JSON object and nothing else:

{
  "outcome": "success" | "failure" | "needs_rework",
  "latency_ms": <integer estimate>,
  "cost": <number estimate>,
  "reasoning": "<one sentence>",
  "confidence": "high" | "medium" | "low"
}
"""


@dataclass
class ReplayObservation:
    """One historical execution, and the simulated counterfactual for it."""

    trace_id: str
    baseline_outcome: str
    baseline_latency_ms: Optional[int]
    baseline_cost: Optional[float]
    simulated_outcome: str
    simulated_latency_ms: Optional[int]
    simulated_cost: Optional[float]
    confidence: str = "medium"
    reasoning: str = ""


@dataclass
class Layer2Result:
    candidate_id: UUID
    tier: int
    comparisons: list[ComparisonResult] = field(default_factory=list)
    n_observations: int = 0
    sufficient_data: bool = False
    notes: list[str] = field(default_factory=list)
    sequential_looks: list[dict] = field(default_factory=list)

    @property
    def tier_label(self) -> str:
        return TIER_LABELS.get(self.tier, "unknown")

    @property
    def value_delivered(self) -> Optional[float]:
        """
        Improvement on the primary metric (success rate), for the
        pricing/metering field on the scorecard.

        Absolute delta, not relative. success_rate lives in [0, 1], so the
        absolute change is directly interpretable as percentage points —
        and relative change is *undefined* exactly where the improvement
        is largest: a workflow going from 0% to 100% success has an
        infinite relative delta, which would otherwise silently report as
        no value at all.

        Returns None unless the improvement is statistically real —
        billing off a delta indistinguishable from noise would be
        indefensible, and this field feeds a commercial conversation.
        """
        for c in self.comparisons:
            if c.metric == "success_rate" and c.verdict == Verdict.BETTER:
                return c.delta
        return None

    def to_metrics_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "tier_label": self.tier_label,
            "n_observations": self.n_observations,
            "sufficient_data": self.sufficient_data,
            "notes": self.notes,
            "comparisons": [
                {
                    "metric": c.metric,
                    "baseline_mean": round(c.baseline_mean, 4),
                    "candidate_mean": round(c.candidate_mean, 4),
                    "delta": round(c.delta, 4),
                    "ci_lower": round(c.delta_ci.lower, 4),
                    "ci_upper": round(c.delta_ci.upper, 4),
                    "p_value": round(c.p_value, 6),
                    "verdict": c.verdict.value,
                    "n": c.n_candidate,
                }
                for c in self.comparisons
            ],
            "sequential_looks": self.sequential_looks,
        }


def _outcome_to_success(outcome: str) -> float:
    return 1.0 if outcome == "success" else 0.0


def _outcome_to_rework(outcome: str) -> float:
    return 1.0 if outcome == "needs_rework" else 0.0


class SimulatedReplayEvaluator:
    """
    Tier 3. Cheapest and weakest of the three tiers.

    Uses a single model rather than the debate panel: this is estimation,
    not adjudication, and running a panel per historical trace would
    multiply cost with no corresponding gain in evidence quality — the
    ceiling here is set by it being simulation at all, not by how many
    models agree on the simulation.
    """

    def __init__(self, pool: asyncpg.Pool, agent: PanelAgent, on_call=None):
        self._pool = pool
        self._agent = agent
        self.on_call = on_call  # cost-recording hook, see debate/engine.py

    async def _fetch_traces(self, task_node_id: UUID, limit: int) -> list[asyncpg.Record]:
        return await self._pool.fetch(
            "SELECT trace_id, outcome, latency_ms, cost, timestamp "
            "FROM traces WHERE task_node_id = $1 "
            "ORDER BY timestamp DESC LIMIT $2",
            task_node_id, limit,
        )

    async def _simulate_one(
        self, trace: asyncpg.Record, change_set: ChangeSet, task_name: str
    ) -> Optional[ReplayObservation]:
        ops = json.dumps([op.model_dump(mode="json") for op in change_set.ops], indent=2)
        user = f"""\
## Workflow step

{task_name}

## What actually happened on this execution

outcome: {trace['outcome']}
latency_ms: {trace['latency_ms']}
cost: {trace['cost']}

## Proposed change

{ops}

Predict the outcome of this same execution had the change been in place.
"""
        try:
            raw = await self._agent.respond(SIMULATION_SYSTEM, user)
            if self.on_call:
                await self.on_call(self._agent, SIMULATION_SYSTEM + user, raw)
            payload = _extract_json(raw)
        except Exception as exc:  # noqa: BLE001
            # One failed simulation is a dropped observation, not a failed
            # evaluation. Reducing n is honest; substituting a guess is not.
            log.warning("simulation failed for trace %s: %s", trace["trace_id"], exc)
            return None

        outcome = payload.get("outcome")
        if outcome not in ("success", "failure", "needs_rework"):
            log.warning("simulation returned invalid outcome %r; dropping", outcome)
            return None

        def _num(key: str, fallback):
            value = payload.get(key)
            return value if isinstance(value, (int, float)) else fallback

        return ReplayObservation(
            trace_id=trace["trace_id"],
            baseline_outcome=trace["outcome"],
            baseline_latency_ms=trace["latency_ms"],
            baseline_cost=float(trace["cost"]) if trace["cost"] is not None else None,
            simulated_outcome=outcome,
            simulated_latency_ms=_num("latency_ms", trace["latency_ms"]),
            simulated_cost=_num("cost", float(trace["cost"]) if trace["cost"] is not None else None),
            confidence=str(payload.get("confidence", "medium")),
            reasoning=str(payload.get("reasoning", ""))[:300],
        )

    async def evaluate(
        self,
        candidate_id: UUID,
        task_node_id: UUID,
        change_set: ChangeSet,
        task_name: str = "",
        max_n: int = 40,
        min_n: int = 8,
        alpha: float = 0.05,
    ) -> Layer2Result:
        result = Layer2Result(candidate_id=candidate_id, tier=TIER_SIMULATED)
        result.notes.append(
            "Tier 3: outcomes are model-estimated counterfactuals, not observed "
            "measurements. Treat as a filter for weak candidates, not as evidence "
            "a change works."
        )

        traces = await self._fetch_traces(task_node_id, max_n)
        if len(traces) < min_n:
            result.notes.append(
                f"Only {len(traces)} historical execution(s) available; "
                f"{min_n} is the minimum for any meaningful comparison. "
                "No statistical claim is made."
            )
            result.n_observations = len(traces)
            return result

        observations: list[ReplayObservation] = []
        for trace in traces:
            obs = await self._simulate_one(trace, change_set, task_name)
            if obs:
                observations.append(obs)

        result.n_observations = len(observations)
        if len(observations) < min_n:
            result.notes.append(
                f"Only {len(observations)} of {len(traces)} simulations succeeded — "
                "too few to compare."
            )
            return result

        result.sufficient_data = True
        result.comparisons = self._compare(observations, alpha=alpha)

        # Multiple-comparisons correction across metrics: testing four
        # metrics at alpha=0.05 each means a ~19% chance of at least one
        # spurious 'significant' result if left uncorrected.
        p_values = [c.p_value for c in result.comparisons]
        rejected = benjamini_hochberg(p_values, alpha=alpha)
        for comparison, keep in zip(result.comparisons, rejected):
            if not keep and comparison.verdict in (Verdict.BETTER, Verdict.WORSE):
                comparison.verdict = Verdict.NO_DETECTABLE_DIFFERENCE
        result.notes.append(
            f"Benjamini-Hochberg correction applied across {len(p_values)} metrics."
        )

        # Report the sample size that *would* have been needed, so an
        # underpowered result is visibly underpowered rather than just
        # 'not significant'.
        for comparison in result.comparisons:
            if comparison.verdict == Verdict.NO_DETECTABLE_DIFFERENCE:
                spread = abs(comparison.baseline_mean) * 0.1 or 1.0
                sigma = (comparison.delta_ci.upper - comparison.delta_ci.lower) / 4 or 1.0
                needed = required_sample_size(sigma=sigma, delta=spread, alpha=alpha)
                if needed > comparison.n_candidate:
                    result.notes.append(
                        f"{comparison.metric}: n={comparison.n_candidate} is "
                        f"underpowered to detect a 10% change; ~{needed} needed."
                    )
        return result

    def _compare(
        self, observations: Sequence[ReplayObservation], alpha: float
    ) -> list[ComparisonResult]:
        comparisons: list[ComparisonResult] = []

        pairs = {
            "success_rate": (
                [_outcome_to_success(o.baseline_outcome) for o in observations],
                [_outcome_to_success(o.simulated_outcome) for o in observations],
            ),
            "rework_rate": (
                [_outcome_to_rework(o.baseline_outcome) for o in observations],
                [_outcome_to_rework(o.simulated_outcome) for o in observations],
            ),
            "latency_ms": (
                [float(o.baseline_latency_ms) for o in observations if o.baseline_latency_ms is not None],
                [float(o.simulated_latency_ms) for o in observations if o.simulated_latency_ms is not None],
            ),
            "cost": (
                [o.baseline_cost for o in observations if o.baseline_cost is not None],
                [o.simulated_cost for o in observations if o.simulated_cost is not None],
            ),
        }

        for metric, (baseline, candidate) in pairs.items():
            if len(baseline) < 2 or len(candidate) < 2:
                continue
            try:
                comparisons.append(
                    welch_comparison(
                        baseline, candidate, metric=metric, alpha=alpha,
                        higher_is_better=METRIC_DIRECTIONS[metric],
                    )
                )
            except ValueError as exc:
                log.warning("could not compare %s: %s", metric, exc)
        return comparisons
