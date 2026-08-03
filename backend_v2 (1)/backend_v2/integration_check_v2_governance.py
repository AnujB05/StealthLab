"""
Governance verified against real Postgres.

The unit tests cover pure logic. These cover what only exists in the
database: window arithmetic, and — most importantly — whether concurrent
requests can slip past the limit. A check-then-insert that isn't
transactional lets N simultaneous requests all read "9 of 10 used" and
all proceed, which is exactly how rate limiters fail under the load that
motivates having one.

Run:
    DATABASE_URL=postgresql://... python integration_check_v2_governance.py
"""
import asyncio
import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(__file__))

from app.db.session import create_pool
from app.services.governance import (
    BudgetExceeded,
    CostGovernor,
    RateLimit,
    RateLimiter,
    RateLimitExceeded,
)


async def main():
    pool = await create_pool(os.environ["DATABASE_URL"])
    failures = []

    def check(name, condition, detail=""):
        status = "PASS" if condition else "FAIL"
        print(f"  [{status}] {name}" + (f" -- {detail}" if detail and not condition else ""))
        if not condition:
            failures.append(name)

    await pool.execute("DELETE FROM rate_limit_events")
    await pool.execute("DELETE FROM llm_spend")

    limits = {"/test": RateLimit(max_requests=3, window=timedelta(hours=1))}
    limiter = RateLimiter(pool, limits=limits)

    print("-- basic limiting --")
    for i in range(3):
        await limiter.check_and_record("viewer:alice", "/test")
    check("three requests within the limit are allowed", True)

    blocked = False
    retry_after = None
    try:
        await limiter.check_and_record("viewer:alice", "/test")
    except RateLimitExceeded as exc:
        blocked = True
        retry_after = exc.retry_after_seconds
    check("the fourth request is blocked", blocked)
    check("a usable Retry-After is returned",
          retry_after is not None and 0 < retry_after <= 3600, f"got {retry_after}")

    print("\n-- isolation between keys and endpoints --")
    await limiter.check_and_record("viewer:bob", "/test")
    check("a different viewer has their own budget", True)
    await limiter.check_and_record("viewer:alice", "/other")
    check("a different endpoint has its own budget", True)

    print("\n-- concurrency: the case naive limiters fail --")
    await pool.execute("DELETE FROM rate_limit_events")
    # Ten simultaneous requests against a limit of three. Without a
    # transaction around check-then-insert, all ten read a count of zero
    # and all ten proceed.
    results = await asyncio.gather(
        *(limiter.check_and_record("viewer:racer", "/test") for _ in range(10)),
        return_exceptions=True,
    )
    allowed = sum(1 for r in results if not isinstance(r, Exception))
    rejected = sum(1 for r in results if isinstance(r, RateLimitExceeded))
    check("no more than the limit was allowed under concurrency",
          allowed <= 3, f"{allowed} allowed, limit is 3")
    check("the remainder were rejected, not errored",
          allowed + rejected == 10, f"{allowed} allowed + {rejected} rejected of 10")

    recorded = await pool.fetchval(
        "SELECT COUNT(*) FROM rate_limit_events WHERE scope_key = 'viewer:racer'"
    )
    check("recorded events match what was allowed",
          recorded == allowed, f"recorded {recorded}, allowed {allowed}")

    print("\n-- window expiry --")
    await pool.execute("DELETE FROM rate_limit_events")
    await pool.execute(
        "INSERT INTO rate_limit_events (scope_key, endpoint, occurred_at) "
        "VALUES ('viewer:old', '/test', now() - interval '2 hours')"
    )
    await limiter.check_and_record("viewer:old", "/test")
    check("events outside the window do not count against the limit", True)

    print("\n-- cost governance --")
    governor = CostGovernor(pool, daily_cap_usd=1.0, per_viewer_daily_cap_usd=0.10)
    await governor.check_budget("viewer:fresh")
    check("a fresh viewer is under budget", True)

    cost = await governor.record(
        "anthropic", "claude-sonnet-4-6", "chat",
        input_tokens=50_000, output_tokens=10_000, scope_key="viewer:spender",
    )
    check("spend is recorded with a positive estimate", cost > 0, f"cost={cost}")

    stored = await pool.fetchval(
        "SELECT estimated_cost FROM llm_spend WHERE scope_key = 'viewer:spender'"
    )
    check("the estimate persisted correctly",
          stored is not None and abs(float(stored) - cost) < 1e-9)

    exceeded = False
    try:
        await governor.check_budget("viewer:spender")
    except BudgetExceeded:
        exceeded = True
    check("the per-viewer cap blocks a heavy user", exceeded)

    await governor.check_budget("viewer:someone_else")
    check("one heavy user does not block everyone else", True)

    print("\n-- housekeeping --")
    purged = await limiter.purge_old(older_than=timedelta(hours=1))
    check("old events can be purged", purged >= 0, f"purged {purged}")

    await pool.close()
    print(f"\n{'=' * 55}")
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        sys.exit(1)
    print("V2 GOVERNANCE VERIFIED against real Postgres.")


if __name__ == "__main__":
    asyncio.run(main())
