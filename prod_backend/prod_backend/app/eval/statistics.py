"""
Statistical machinery for Layer 2 empirical evaluation.

Deliberately separated from anything that generates the numbers (shadow
deployment, off-policy evaluation, or simulated replay). Those tiers
differ in evidence quality; the statistics applied to them do not.

Three things this exists to prevent, all of which are easy to get wrong
and produce confident-looking garbage:

  1. **Winner's curse.** Test enough candidates and one looks best purely
     from noise. Handled by multiple-comparisons correction.

  2. **Peeking.** Checking results repeatedly and stopping when they look
     significant inflates the false-positive rate far above the nominal
     alpha. Handled by alpha-spending boundaries.

  3. **Underpowered confidence.** Declaring "no difference" from a sample
     too small to detect the difference you care about. Handled by
     explicit power analysis, and by reporting "inconclusive" as a
     distinct outcome from "no effect".

All functions here are pure — no I/O, no model calls — so they can be
tested against known-correct reference values.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Sequence

from scipy import stats


class Verdict(str, Enum):
    BETTER = "better"
    WORSE = "worse"
    NO_DETECTABLE_DIFFERENCE = "no_detectable_difference"
    INCONCLUSIVE = "inconclusive"  # ran out of budget before resolving


@dataclass(frozen=True)
class ConfidenceInterval:
    lower: float
    upper: float
    confidence: float

    @property
    def excludes_zero(self) -> bool:
        return self.lower > 0 or self.upper < 0

    def __str__(self) -> str:
        return f"[{self.lower:.4f}, {self.upper:.4f}] @ {self.confidence:.0%}"


@dataclass
class ComparisonResult:
    """Candidate vs. baseline on one metric."""

    metric: str
    baseline_mean: float
    candidate_mean: float
    delta: float
    delta_ci: ConfidenceInterval
    p_value: float
    n_baseline: int
    n_candidate: int
    verdict: Verdict
    higher_is_better: bool = False

    @property
    def relative_delta(self) -> Optional[float]:
        if self.baseline_mean == 0:
            return None
        return self.delta / abs(self.baseline_mean)


def _sample_variance(values: Sequence[float]) -> float:
    """
    Two-pass sample variance (Bessel-corrected).

    Not scipy.tvar: that computes variance via moments and hits
    catastrophic cancellation on near-constant data, emitting precision
    warnings and unreliable results. Near-constant data is exactly what
    deterministic replay metrics look like, so this path matters here
    rather than being a theoretical edge case. The two-pass form is
    numerically stable for that case.
    """
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    return sum((x - mean) ** 2 for x in values) / (n - 1)


def required_sample_size(
    sigma: float, delta: float, alpha: float = 0.05, power: float = 0.80
) -> int:
    """
    Two-sample size per arm:  n = 2(z_{alpha/2} + z_beta)^2 sigma^2 / delta^2

    `delta` is the *minimum effect worth detecting*, not any effect. Set
    it from what actually matters operationally — asking for the power to
    detect an improvement too small to care about is the most common way
    this calculation produces an absurd n.
    """
    if delta <= 0:
        raise ValueError("delta (minimum detectable effect) must be positive")
    if sigma < 0:
        raise ValueError("sigma must be non-negative")
    if sigma == 0:
        return 2  # a deterministic metric needs only enough to compare

    z_alpha = stats.norm.ppf(1 - alpha / 2)
    z_beta = stats.norm.ppf(power)
    n = 2 * (z_alpha + z_beta) ** 2 * sigma**2 / delta**2
    return max(2, math.ceil(n))


def welch_comparison(
    baseline: Sequence[float],
    candidate: Sequence[float],
    metric: str = "metric",
    alpha: float = 0.05,
    higher_is_better: bool = False,
) -> ComparisonResult:
    """
    Welch's t-test — does NOT assume equal variances.

    Student's t would be the wrong default here: a candidate that changes
    a workflow can easily change the *variance* of an outcome as well as
    its mean (e.g. caching makes latency both lower and more consistent),
    and Student's t is anti-conservative exactly in that case.
    """
    n1, n2 = len(baseline), len(candidate)
    if n1 < 2 or n2 < 2:
        raise ValueError("need at least 2 observations per arm")

    mean1 = float(sum(baseline) / n1)
    mean2 = float(sum(candidate) / n2)
    var1 = _sample_variance(baseline)
    var2 = _sample_variance(candidate)

    se = math.sqrt(var1 / n1 + var2 / n2)
    delta = mean2 - mean1

    if se == 0:
        # Both arms deterministic. A difference is real; no difference is real.
        p_value = 0.0 if delta != 0 else 1.0
        df = float(n1 + n2 - 2)
        ci = ConfidenceInterval(delta, delta, 1 - alpha)
    else:
        # Welch-Satterthwaite degrees of freedom
        df = (var1 / n1 + var2 / n2) ** 2 / (
            (var1 / n1) ** 2 / (n1 - 1) + (var2 / n2) ** 2 / (n2 - 1)
        )
        t_stat = delta / se
        p_value = float(2 * stats.t.sf(abs(t_stat), df))
        t_crit = float(stats.t.ppf(1 - alpha / 2, df))
        ci = ConfidenceInterval(delta - t_crit * se, delta + t_crit * se, 1 - alpha)

    if p_value >= alpha:
        verdict = Verdict.NO_DETECTABLE_DIFFERENCE
    else:
        improved = delta > 0 if higher_is_better else delta < 0
        verdict = Verdict.BETTER if improved else Verdict.WORSE

    return ComparisonResult(
        metric=metric,
        baseline_mean=mean1,
        candidate_mean=mean2,
        delta=delta,
        delta_ci=ci,
        p_value=p_value,
        n_baseline=n1,
        n_candidate=n2,
        verdict=verdict,
        higher_is_better=higher_is_better,
    )


def obrien_fleming_boundary(information_fraction: float, alpha: float = 0.05) -> float:
    """
    Lan-DeMets alpha-spending with an O'Brien-Fleming boundary: the
    two-sided p-value threshold permitted at this interim look.

    O'Brien-Fleming rather than Pocock because it spends alpha very
    conservatively early — an early look needs overwhelming evidence to
    stop. That matches the situation here: stopping early on a workflow
    change that later proves wrong is expensive, and the cost of a few
    extra replay runs is not.

    At t=1 (the final look) this returns approximately the nominal alpha.
    """
    if not 0 < information_fraction <= 1:
        raise ValueError("information_fraction must be in (0, 1]")

    z_alpha = stats.norm.ppf(1 - alpha / 2)
    z_boundary = z_alpha / math.sqrt(information_fraction)
    return float(2 * stats.norm.sf(z_boundary))


def benjamini_hochberg(
    p_values: Sequence[float], alpha: float = 0.05
) -> list[bool]:
    """
    Benjamini-Hochberg FDR control. Returns per-input reject/accept flags,
    in the caller's original order.

    BH rather than Bonferroni: Bonferroni controls the probability of
    *any* false positive, which is far stricter than needed when
    comparing a handful of workflow candidates — it would suppress real
    improvements to avoid one spurious one. BH controls the expected
    *proportion* of false discoveries, the more useful guarantee here.
    """
    m = len(p_values)
    if m == 0:
        return []

    indexed = sorted(enumerate(p_values), key=lambda kv: kv[1])
    max_k = -1
    for rank, (_, p) in enumerate(indexed, start=1):
        if p <= (rank / m) * alpha:
            max_k = rank

    rejected = [False] * m
    if max_k > 0:
        for rank, (original_index, _) in enumerate(indexed, start=1):
            if rank <= max_k:
                rejected[original_index] = True
    return rejected


@dataclass
class SequentialPlan:
    """
    Budgeted sequential test: run in batches, stop as soon as the evidence
    is decisive, cap total spend.

    Hitting the cap without resolving is a *real result* — reported as
    INCONCLUSIVE rather than being forced into a binary. Forcing a verdict
    from an underpowered sample is exactly the failure this is meant to
    avoid.
    """

    max_n: int
    batch_size: int = 20
    alpha: float = 0.05
    min_effect: Optional[float] = None
    higher_is_better: bool = False

    looks: list[dict] = field(default_factory=list)

    def evaluate_look(
        self, baseline: Sequence[float], candidate: Sequence[float], metric: str = "metric"
    ) -> tuple[bool, ComparisonResult]:
        """
        Assess one interim look. Returns (should_stop, result).

        The comparison is computed at the nominal alpha for reporting, but
        the stopping decision uses the alpha-spending boundary — so the
        confidence interval shown to a human stays interpretable while the
        stop rule stays statistically honest.
        """
        n = min(len(baseline), len(candidate))
        information_fraction = min(1.0, n / self.max_n) if self.max_n > 0 else 1.0
        boundary = obrien_fleming_boundary(information_fraction, self.alpha)

        result = welch_comparison(
            baseline, candidate, metric=metric, alpha=self.alpha,
            higher_is_better=self.higher_is_better,
        )

        crossed = result.p_value <= boundary
        exhausted = n >= self.max_n

        self.looks.append({
            "n": n,
            "information_fraction": round(information_fraction, 4),
            "boundary_p": boundary,
            "observed_p": result.p_value,
            "crossed": crossed,
        })

        if crossed:
            return True, result
        if exhausted:
            # Ran the full budget without crossing. Distinguish "we looked
            # hard and found nothing" from "we gave up early".
            if self.min_effect is not None and result.delta_ci.upper < self.min_effect:
                result.verdict = Verdict.NO_DETECTABLE_DIFFERENCE
            else:
                result.verdict = Verdict.INCONCLUSIVE
            return True, result

        return False, result
