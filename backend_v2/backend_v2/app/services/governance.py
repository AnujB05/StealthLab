"""
Rate limiting and LLM cost governance (V2).

Two separate protections against the same class of problem, kept
separate because they fail differently:

  - **Rate limiting** bounds request *frequency*. Cheap to check, and
    the right tool against scripted abuse.

  - **Cost governance** bounds actual *spend*. A single `/v1/admin/scan`
    fans out across a debate panel, a judge, and Layer 2 simulations —
    dozens of model calls from one request. A rate limit of "10 scans an
    hour" says nothing useful about the resulting bill, which is why a
    frequency cap alone is not enough.

Both fail *closed* on infrastructure errors. A rate limiter that
silently allows everything when its backing store is unreachable is
worse than no rate limiter, because it creates false confidence.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import asyncpg

log = logging.getLogger(__name__)


class RateLimitExceeded(Exception):
    def __init__(self, message: str, retry_after_seconds: int):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class BudgetExceeded(Exception):
    pass


@dataclass(frozen=True)
class RateLimit:
    max_requests: int
    window: timedelta

    @property
    def window_seconds(self) -> int:
        return int(self.window.total_seconds())


# Defaults are deliberately conservative. These are guardrails against
# abuse and runaway cost, not capacity planning — raise them from
# observed usage rather than guessing upward.
DEFAULT_LIMITS: dict[str, RateLimit] = {
    # Expensive: fans out to a full debate panel plus judge plus Layer 2.
    "/v1/admin/scan": RateLimit(max_requests=5, window=timedelta(hours=1)),
    # Moderate: one model call plus retrieval per request.
    "/v1/chat": RateLimit(max_requests=30, window=timedelta(hours=1)),
    # Cheap, but unbounded ingestion is still a denial-of-service vector.
    "/v1/traces": RateLimit(max_requests=120, window=timedelta(hours=1)),
}


# Rough USD per million tokens. Deliberately approximate and deliberately
# rounded *up*: this drives a spending guardrail, so overestimating stops
# spend slightly early while underestimating lets it overshoot the cap.
# Local models are free, hence zero.
_PRICE_PER_MTOK: dict[str, tuple[float, float]] = {
    "anthropic": (3.0, 15.0),
    "openai": (2.5, 10.0),
    "google": (1.5, 6.0),
    "moonshot": (1.0, 3.0),
    "voyage": (0.12, 0.0),
    "local": (0.0, 0.0),
    "mock": (0.0, 0.0),
}


def estimate_cost(
    provider: str, input_tokens: int = 0, output_tokens: int = 0
) -> float:
    """
    Estimated USD for one call.

    An unknown provider is priced at the most expensive known rate rather
    than zero — an unrecognised model should not be able to spend
    invisibly just because it wasn't in the table.
    """
    key = provider.lower()
    if key in _PRICE_PER_MTOK:
        price_in, price_out = _PRICE_PER_MTOK[key]
    else:
        log.warning("unknown provider %r for cost estimation; using worst-case rate", provider)
        price_in = max(p[0] for p in _PRICE_PER_MTOK.values())
        price_out = max(p[1] for p in _PRICE_PER_MTOK.values())

    return (input_tokens * price_in + output_tokens * price_out) / 1_000_000


def estimate_tokens(text: str) -> int:
    """
    Crude token estimate for when a provider doesn't return usage.

    ~4 characters per token is the standard rough heuristic for English.
    Wrong for code and non-Latin scripts, which is acceptable for a
    guardrail but would not be for billing.
    """
    return max(1, len(text) // 4)


class RateLimiter:
    def __init__(self, pool: asyncpg.Pool, limits: Optional[dict[str, RateLimit]] = None):
        self._pool = pool
        self._limits = limits if limits is not None else DEFAULT_LIMITS

    async def check_and_record(self, scope_key: str, endpoint: str) -> None:
        """
        Raise RateLimitExceeded if over the limit; otherwise record this
        request.

        Check and record happen in one transaction so two concurrent
        requests can't both observe "9 of 10 used" and both proceed.
        """
        limit = self._limits.get(endpoint)
        if limit is None:
            return

        since = datetime.now(timezone.utc) - limit.window

        try:
            async with self._pool.acquire() as conn:
                async with conn.transaction():
                    # A transaction alone is NOT sufficient here. Under
                    # Postgres's default READ COMMITTED isolation, N
                    # concurrent requests each see a snapshot without the
                    # others' uncommitted inserts, so all N read the same
                    # count and all N proceed -- verified failing before
                    # this lock was added.
                    #
                    # The advisory lock serialises checks for one key
                    # only, so unrelated keys still run concurrently. It
                    # releases automatically at transaction end, including
                    # on error. SERIALIZABLE isolation would also work but
                    # would surface serialisation failures needing retry
                    # logic at every call site.
                    await conn.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                        f"ratelimit:{scope_key}:{endpoint}",
                    )
                    count = await conn.fetchval(
                        "SELECT COUNT(*) FROM rate_limit_events "
                        "WHERE scope_key = $1 AND endpoint = $2 AND occurred_at >= $3",
                        scope_key, endpoint, since,
                    )
                    if count >= limit.max_requests:
                        oldest = await conn.fetchval(
                            "SELECT MIN(occurred_at) FROM rate_limit_events "
                            "WHERE scope_key = $1 AND endpoint = $2 AND occurred_at >= $3",
                            scope_key, endpoint, since,
                        )
                        retry_after = limit.window_seconds
                        if oldest:
                            elapsed = (datetime.now(timezone.utc) - oldest).total_seconds()
                            retry_after = max(1, int(limit.window_seconds - elapsed))
                        raise RateLimitExceeded(
                            f"{limit.max_requests} requests per "
                            f"{limit.window_seconds // 60} minutes allowed for {endpoint}",
                            retry_after_seconds=retry_after,
                        )
                    await conn.execute(
                        "INSERT INTO rate_limit_events (scope_key, endpoint) VALUES ($1, $2)",
                        scope_key, endpoint,
                    )
        except RateLimitExceeded:
            raise
        except Exception as exc:  # noqa: BLE001
            # Fail closed. A limiter that lets everything through when its
            # store is unreachable provides no protection while appearing
            # to, which is the worst of both.
            log.error("rate limiter unavailable, denying request: %s", exc)
            raise RateLimitExceeded(
                "rate limiting is temporarily unavailable", retry_after_seconds=60
            ) from exc

    async def purge_old(self, older_than: timedelta = timedelta(days=2)) -> int:
        """Housekeeping — the events table is append-only and unbounded."""
        cutoff = datetime.now(timezone.utc) - older_than
        result = await self._pool.execute(
            "DELETE FROM rate_limit_events WHERE occurred_at < $1", cutoff
        )
        return int(result.split()[-1]) if result else 0


class CostGovernor:
    """
    Bounds actual LLM spend over a rolling window.

    Checked *before* a workload starts, and recorded *after* each call.
    That ordering means a single very expensive workload can overshoot
    the cap — the check can only see spend already recorded. Reducing
    that gap would mean pre-estimating each workload's total cost, which
    is not reliably knowable for a variable-round debate. The cap is
    therefore a bound on sustained spend, not a hard per-request ceiling.
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        daily_cap_usd: float = 10.0,
        per_viewer_daily_cap_usd: float = 1.0,
    ):
        self._pool = pool
        self._daily_cap = daily_cap_usd
        self._per_viewer_cap = per_viewer_daily_cap_usd

    async def spend_since(
        self, since: datetime, scope_key: Optional[str] = None
    ) -> float:
        if scope_key:
            value = await self._pool.fetchval(
                "SELECT COALESCE(SUM(estimated_cost), 0) FROM llm_spend "
                "WHERE occurred_at >= $1 AND scope_key = $2",
                since, scope_key,
            )
        else:
            value = await self._pool.fetchval(
                "SELECT COALESCE(SUM(estimated_cost), 0) FROM llm_spend "
                "WHERE occurred_at >= $1",
                since,
            )
        return float(value or 0)

    async def check_budget(self, scope_key: Optional[str] = None) -> None:
        """Raise BudgetExceeded if either the global or per-viewer cap is hit."""
        since = datetime.now(timezone.utc) - timedelta(days=1)

        try:
            total = await self.spend_since(since)
            if total >= self._daily_cap:
                raise BudgetExceeded(
                    f"daily LLM budget of ${self._daily_cap:.2f} reached "
                    f"(estimated ${total:.2f} spent in the last 24h)"
                )
            if scope_key:
                viewer_total = await self.spend_since(since, scope_key)
                if viewer_total >= self._per_viewer_cap:
                    raise BudgetExceeded(
                        f"your daily budget of ${self._per_viewer_cap:.2f} is reached "
                        f"(estimated ${viewer_total:.2f} spent in the last 24h)"
                    )
        except BudgetExceeded:
            raise
        except Exception as exc:  # noqa: BLE001
            log.error("cost governor unavailable, denying request: %s", exc)
            raise BudgetExceeded("cost governance is temporarily unavailable") from exc

    async def record(
        self,
        provider: str,
        model: str,
        operation: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        scope_key: Optional[str] = None,
    ) -> float:
        cost = estimate_cost(provider, input_tokens, output_tokens)
        try:
            await self._pool.execute(
                "INSERT INTO llm_spend (scope_key, provider, model, operation, "
                "estimated_cost, input_tokens, output_tokens) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7)",
                scope_key, provider, model, operation, cost, input_tokens, output_tokens,
            )
        except Exception as exc:  # noqa: BLE001
            # Unlike the checks, recording fails *open*: the model call has
            # already happened and been paid for, so raising here would
            # discard completed work over a bookkeeping error. Logged
            # loudly because unrecorded spend erodes the cap's accuracy.
            log.error("failed to record LLM spend (%s/%s): %s", provider, model, exc)
        return cost
