"""
Tests for rate limiting and cost governance (V2).

The pure logic (cost estimation, limit configuration) is tested here.
Concurrency and transactional correctness are properties of the SQL and
are tested against real Postgres in integration_check_v2_governance.py.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.governance import (
    DEFAULT_LIMITS,
    BudgetExceeded,
    CostGovernor,
    RateLimit,
    RateLimiter,
    RateLimitExceeded,
    estimate_cost,
    estimate_tokens,
)


# --- Cost estimation ---

def test_cost_scales_with_tokens():
    cheap = estimate_cost("anthropic", input_tokens=1000, output_tokens=0)
    expensive = estimate_cost("anthropic", input_tokens=1_000_000, output_tokens=0)
    assert expensive == pytest.approx(cheap * 1000)


def test_output_tokens_cost_more_than_input():
    """Every major provider prices output above input; the table must reflect that."""
    for provider in ("anthropic", "openai", "google", "moonshot"):
        in_cost = estimate_cost(provider, input_tokens=1_000_000)
        out_cost = estimate_cost(provider, output_tokens=1_000_000)
        assert out_cost > in_cost, f"{provider} output should cost more"


def test_local_models_are_free():
    assert estimate_cost("local", input_tokens=10_000_000, output_tokens=10_000_000) == 0.0
    assert estimate_cost("mock", input_tokens=1_000_000) == 0.0


def test_unknown_provider_priced_at_worst_case_not_zero():
    """
    An unrecognised provider must not be able to spend invisibly. Pricing
    it at zero would exempt exactly the models nobody configured limits
    for.
    """
    unknown = estimate_cost("some-new-provider", input_tokens=1_000_000)
    known_max = max(
        estimate_cost(p, input_tokens=1_000_000)
        for p in ("anthropic", "openai", "google", "moonshot")
    )
    assert unknown >= known_max
    assert unknown > 0


def test_zero_tokens_costs_nothing():
    assert estimate_cost("anthropic") == 0.0


def test_token_estimate_is_roughly_four_chars_per_token():
    assert estimate_tokens("a" * 400) == 100
    assert estimate_tokens("") == 1  # never zero -- a call always costs something


# --- Limit configuration ---

def test_expensive_endpoint_has_the_tightest_limit():
    """
    /v1/admin/scan fans out to a whole panel plus judge plus Layer 2, so
    it must be limited far more tightly than a single-call endpoint.
    """
    scan = DEFAULT_LIMITS["/v1/admin/scan"]
    chat = DEFAULT_LIMITS["/v1/chat"]
    traces = DEFAULT_LIMITS["/v1/traces"]
    assert scan.max_requests < chat.max_requests < traces.max_requests


def test_unlimited_endpoint_is_not_rate_limited():
    """An endpoint with no configured limit must pass through untouched."""
    pool = MagicMock()
    limiter = RateLimiter(pool, limits={})
    asyncio.run(limiter.check_and_record("viewer:alice", "/v1/anything"))
    pool.acquire.assert_not_called()


# --- Fail-closed behaviour ---

def _failing_pool():
    pool = MagicMock()
    pool.acquire.side_effect = RuntimeError("database unreachable")
    return pool


def test_rate_limiter_fails_closed_when_store_unreachable():
    """
    A limiter that allows everything when its store is down provides no
    protection while appearing to -- the worst outcome.
    """
    limiter = RateLimiter(_failing_pool(), limits=DEFAULT_LIMITS)
    with pytest.raises(RateLimitExceeded, match="temporarily unavailable"):
        asyncio.run(limiter.check_and_record("viewer:alice", "/v1/chat"))


def test_cost_governor_fails_closed_when_store_unreachable():
    pool = MagicMock()
    pool.fetchval = AsyncMock(side_effect=RuntimeError("database unreachable"))
    governor = CostGovernor(pool)
    with pytest.raises(BudgetExceeded, match="temporarily unavailable"):
        asyncio.run(governor.check_budget("viewer:alice"))


def test_spend_recording_fails_open():
    """
    Opposite direction on purpose: the model call already happened and was
    already paid for, so a bookkeeping failure must not discard completed
    work by raising.
    """
    pool = MagicMock()
    pool.execute = AsyncMock(side_effect=RuntimeError("insert failed"))
    governor = CostGovernor(pool)
    cost = asyncio.run(
        governor.record("anthropic", "claude", "chat", input_tokens=100)
    )
    assert cost > 0  # returns the estimate despite the write failing


# --- Budget enforcement ---

def _pool_with_spend(total: float, viewer_total: float = 0.0):
    pool = MagicMock()
    calls = {"n": 0}

    async def fetchval(query, *args):
        calls["n"] += 1
        return total if calls["n"] == 1 else viewer_total

    pool.fetchval = fetchval
    return pool


def test_budget_allows_when_under_cap():
    governor = CostGovernor(_pool_with_spend(2.0, 0.1), daily_cap_usd=10.0)
    asyncio.run(governor.check_budget("viewer:alice"))  # must not raise


def test_global_cap_blocks_everyone():
    governor = CostGovernor(_pool_with_spend(10.5), daily_cap_usd=10.0)
    with pytest.raises(BudgetExceeded, match="daily LLM budget"):
        asyncio.run(governor.check_budget("viewer:alice"))


def test_per_viewer_cap_blocks_one_user_without_blocking_the_system():
    """One heavy user must not be able to exhaust the global budget."""
    governor = CostGovernor(
        _pool_with_spend(3.0, 1.5), daily_cap_usd=10.0, per_viewer_daily_cap_usd=1.0
    )
    with pytest.raises(BudgetExceeded, match="your daily budget"):
        asyncio.run(governor.check_budget("viewer:heavy"))


def test_anonymous_traffic_still_checks_the_global_cap():
    governor = CostGovernor(_pool_with_spend(10.5), daily_cap_usd=10.0)
    with pytest.raises(BudgetExceeded):
        asyncio.run(governor.check_budget(None))
