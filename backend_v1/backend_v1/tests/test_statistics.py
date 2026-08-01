"""
Tests for Layer 2's statistical machinery.

These deliberately validate against *known-correct reference values*
(scipy's own implementations, worked textbook examples, and analytically
derivable cases) rather than only checking the code runs. Statistical
code that runs happily while being subtly wrong is the specific failure
mode worth guarding against here — it produces confident numbers a human
will act on.
"""
from __future__ import annotations

import math

import pytest
from scipy import stats

from app.eval.statistics import (
    ConfidenceInterval,
    SequentialPlan,
    Verdict,
    benjamini_hochberg,
    obrien_fleming_boundary,
    required_sample_size,
    welch_comparison,
)


# --- Sample size: validate against the closed-form standard result ---

def test_sample_size_matches_textbook_formula():
    """
    Standard result: for alpha=0.05, power=0.80, the multiplier
    2*(1.96 + 0.8416)^2 ~= 15.68. With sigma == delta, n ~= 16 per arm.
    This is the canonical worked example in most power-analysis texts.
    """
    n = required_sample_size(sigma=1.0, delta=1.0, alpha=0.05, power=0.80)
    assert n == 16, f"expected 16, got {n}"


def test_sample_size_scales_with_inverse_square_of_effect():
    """Halving the detectable effect should roughly quadruple n."""
    n_large = required_sample_size(sigma=1.0, delta=1.0)
    n_small = required_sample_size(sigma=1.0, delta=0.5)
    assert 3.8 < n_small / n_large < 4.2, f"ratio {n_small / n_large}"


def test_sample_size_scales_with_variance():
    """Doubling sigma should roughly quadruple n."""
    n1 = required_sample_size(sigma=1.0, delta=1.0)
    n2 = required_sample_size(sigma=2.0, delta=1.0)
    assert 3.8 < n2 / n1 < 4.2


def test_higher_power_requires_more_samples():
    assert required_sample_size(sigma=1.0, delta=1.0, power=0.95) > required_sample_size(
        sigma=1.0, delta=1.0, power=0.80
    )


def test_sample_size_rejects_nonsensical_effect():
    with pytest.raises(ValueError):
        required_sample_size(sigma=1.0, delta=0.0)
    with pytest.raises(ValueError):
        required_sample_size(sigma=1.0, delta=-1.0)


# --- Welch's t-test: validate against scipy's own implementation ---

def test_welch_p_value_matches_scipy_exactly():
    """The p-value must match scipy.stats.ttest_ind(equal_var=False)."""
    a = [12.1, 11.8, 13.0, 12.5, 11.9, 12.2, 12.8, 11.5]
    b = [10.2, 9.8, 10.9, 10.1, 9.5, 10.4, 10.0, 9.9]

    result = welch_comparison(a, b)
    expected = stats.ttest_ind(b, a, equal_var=False)  # candidate=b vs baseline=a

    assert result.p_value == pytest.approx(float(expected.pvalue), rel=1e-9)


def test_welch_handles_unequal_variance_correctly():
    """
    The case Welch exists for: same means, wildly different variances.
    Student's t would be anti-conservative here; Welch should not
    report significance.
    """
    tight = [10.0, 10.1, 9.9, 10.0, 10.1, 9.9, 10.0, 10.0]
    loose = [10.0, 15.0, 5.0, 12.0, 8.0, 14.0, 6.0, 10.0]
    result = welch_comparison(tight, loose)
    assert result.verdict == Verdict.NO_DETECTABLE_DIFFERENCE
    assert result.p_value > 0.05


def test_confidence_interval_contains_the_delta():
    a = [5.0, 5.2, 4.8, 5.1, 4.9, 5.0, 5.3, 4.7]
    b = [3.0, 3.2, 2.8, 3.1, 2.9, 3.0, 3.3, 2.7]
    result = welch_comparison(a, b)
    assert result.delta_ci.lower < result.delta < result.delta_ci.upper


def test_clear_improvement_on_lower_is_better_metric():
    """Latency dropping from ~500ms to ~200ms is unambiguously better."""
    baseline = [500.0, 510.0, 495.0, 505.0, 498.0, 502.0, 507.0, 493.0]
    candidate = [200.0, 210.0, 195.0, 205.0, 198.0, 202.0, 207.0, 193.0]
    result = welch_comparison(baseline, candidate, metric="latency_ms",
                              higher_is_better=False)
    assert result.verdict == Verdict.BETTER
    assert result.delta < 0
    assert result.delta_ci.excludes_zero


def test_direction_flips_with_higher_is_better():
    """The same numbers must give the opposite verdict for a success rate."""
    baseline = [0.5, 0.52, 0.48, 0.51, 0.49, 0.50, 0.53, 0.47]
    candidate = [0.9, 0.92, 0.88, 0.91, 0.89, 0.90, 0.93, 0.87]

    as_rate = welch_comparison(baseline, candidate, higher_is_better=True)
    as_cost = welch_comparison(baseline, candidate, higher_is_better=False)

    assert as_rate.verdict == Verdict.BETTER
    assert as_cost.verdict == Verdict.WORSE
    assert as_rate.p_value == as_cost.p_value  # same evidence, different meaning


def test_identical_deterministic_samples_report_no_difference():
    same = [1.0] * 5
    result = welch_comparison(same, list(same))
    assert result.verdict == Verdict.NO_DETECTABLE_DIFFERENCE
    assert result.delta == 0.0


def test_different_deterministic_samples_report_difference():
    result = welch_comparison([1.0] * 5, [2.0] * 5, higher_is_better=True)
    assert result.verdict == Verdict.BETTER
    assert result.p_value == 0.0


def test_welch_requires_minimum_observations():
    with pytest.raises(ValueError):
        welch_comparison([1.0], [2.0, 3.0])


def test_relative_delta_guards_against_zero_baseline():
    result = welch_comparison([0.0, 0.0, 0.0], [1.0, 1.0, 1.0])
    assert result.relative_delta is None


# --- Alpha spending ---

def test_obrien_fleming_is_strict_early_and_relaxes_to_alpha():
    """
    The defining property: an early look demands overwhelming evidence,
    and the final look approaches the nominal alpha.
    """
    early = obrien_fleming_boundary(0.25, alpha=0.05)
    mid = obrien_fleming_boundary(0.5, alpha=0.05)
    final = obrien_fleming_boundary(1.0, alpha=0.05)

    assert early < mid < final
    assert early < 0.001, f"early boundary should be very strict, got {early}"
    assert final == pytest.approx(0.05, rel=1e-6)


def test_obrien_fleming_boundary_derivable_by_hand():
    """At t=0.25, z-boundary = 1.96/sqrt(0.25) = 3.92; check against normal sf."""
    expected = 2 * stats.norm.sf(stats.norm.ppf(0.975) / math.sqrt(0.25))
    assert obrien_fleming_boundary(0.25) == pytest.approx(float(expected), rel=1e-12)


def test_obrien_fleming_rejects_invalid_fraction():
    for bad in (0.0, -0.5, 1.5):
        with pytest.raises(ValueError):
            obrien_fleming_boundary(bad)


# --- Benjamini-Hochberg: validated against a worked example ---

def test_benjamini_hochberg_worked_example():
    """
    Classic worked example. p = [0.005, 0.011, 0.02, 0.04, 0.13], m=5,
    alpha=0.05. Thresholds k/m*alpha = [0.01, 0.02, 0.03, 0.04, 0.05].
    Largest k with p_(k) <= threshold is k=4 (0.04 <= 0.04), so the first
    four are rejected.
    """
    p = [0.005, 0.011, 0.02, 0.04, 0.13]
    assert benjamini_hochberg(p, alpha=0.05) == [True, True, True, True, False]


def test_benjamini_hochberg_preserves_input_order():
    """Order must be the caller's, not sorted order — an easy bug to ship."""
    p = [0.13, 0.005, 0.04, 0.011, 0.02]
    rejected = benjamini_hochberg(p, alpha=0.05)
    assert rejected == [False, True, True, True, True]


def test_benjamini_hochberg_is_less_strict_than_bonferroni():
    """
    The reason BH was chosen. With m=10 and p=0.008, Bonferroni's
    threshold (0.005) rejects nothing; BH should still find discoveries.
    """
    p = [0.008] * 3 + [0.9] * 7
    rejected = benjamini_hochberg(p, alpha=0.05)
    bonferroni_threshold = 0.05 / 10
    assert all(x > bonferroni_threshold for x in [0.008])
    assert sum(rejected) >= 3


def test_benjamini_hochberg_rejects_nothing_when_all_null():
    assert benjamini_hochberg([0.6, 0.7, 0.8, 0.99], alpha=0.05) == [False] * 4


def test_benjamini_hochberg_empty_input():
    assert benjamini_hochberg([], alpha=0.05) == []


def test_benjamini_hochberg_single_value_behaves_like_plain_threshold():
    assert benjamini_hochberg([0.04], alpha=0.05) == [True]
    assert benjamini_hochberg([0.06], alpha=0.05) == [False]


# --- Sequential testing ---

def test_sequential_stops_early_on_overwhelming_evidence():
    plan = SequentialPlan(max_n=100, alpha=0.05, higher_is_better=False)
    baseline = [500.0 + i * 0.1 for i in range(30)]
    candidate = [100.0 + i * 0.1 for i in range(30)]

    stop, result = plan.evaluate_look(baseline, candidate)
    assert stop is True
    assert result.verdict == Verdict.BETTER
    assert plan.looks[0]["crossed"] is True


def test_sequential_does_not_stop_early_on_weak_evidence():
    """
    The peeking guard. A marginal p-value that would look 'significant'
    at nominal alpha must NOT trigger an early stop at low information
    fraction.
    """
    plan = SequentialPlan(max_n=1000, alpha=0.05)
    baseline = [10.0, 10.5, 9.5, 10.2, 9.8, 10.1, 9.9, 10.3, 9.7, 10.0]
    candidate = [9.0, 9.5, 8.5, 9.2, 8.8, 9.1, 8.9, 9.3, 8.7, 9.0]

    stop, result = plan.evaluate_look(baseline, candidate)
    look = plan.looks[0]
    assert look["information_fraction"] == 0.01
    assert look["boundary_p"] < 1e-6, "early boundary must be extremely strict"
    assert stop is False, "must not stop on a first peek at 1% information"


def test_sequential_reports_inconclusive_when_budget_exhausted():
    """
    Exhausting the budget without resolving is a real, reportable result
    — not silently forced into a binary verdict.
    """
    plan = SequentialPlan(max_n=10, alpha=0.05)
    import random
    random.seed(7)
    baseline = [random.gauss(10, 1) for _ in range(10)]
    candidate = [random.gauss(10.05, 1) for _ in range(10)]

    stop, result = plan.evaluate_look(baseline, candidate)
    assert stop is True
    assert result.verdict in (Verdict.INCONCLUSIVE, Verdict.NO_DETECTABLE_DIFFERENCE)


def test_sequential_distinguishes_no_effect_from_inconclusive():
    """
    With min_effect set and a CI tight enough to exclude it, the verdict
    should be a genuine 'no detectable difference', not 'inconclusive'.
    """
    plan = SequentialPlan(max_n=8, alpha=0.05, min_effect=100.0)
    baseline = [10.0, 10.1, 9.9, 10.0, 10.1, 9.9, 10.0, 10.0]
    candidate = [10.0, 10.1, 9.9, 10.0, 10.1, 9.9, 10.0, 10.0]

    stop, result = plan.evaluate_look(baseline, candidate)
    assert stop is True
    assert result.verdict == Verdict.NO_DETECTABLE_DIFFERENCE


def test_sequential_records_an_audit_trail_of_looks():
    """Every interim look must be recorded — this is what makes the
    alpha-spending claim auditable after the fact."""
    plan = SequentialPlan(max_n=100)
    for n in (10, 20, 30):
        plan.evaluate_look([10.0] * n, [9.0 + i * 0.01 for i in range(n)])
    assert len(plan.looks) == 3
    fractions = [look["information_fraction"] for look in plan.looks]
    assert fractions == sorted(fractions), "information fraction must be monotonic"


# --- Confidence interval helper ---

def test_ci_excludes_zero_detection():
    assert ConfidenceInterval(0.5, 1.5, 0.95).excludes_zero is True
    assert ConfidenceInterval(-1.5, -0.5, 0.95).excludes_zero is True
    assert ConfidenceInterval(-0.5, 1.5, 0.95).excludes_zero is False
