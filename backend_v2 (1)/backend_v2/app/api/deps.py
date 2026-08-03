"""
Per-request dependencies (V2).

Resolves the viewer identity for a request into an AccessScope. Real
authentication (Tier 1) is not built yet, so identity currently comes
from a header — which is fine for a public commons where nothing is
private, and is *not* fine the moment private visibility is enabled.

The header is trusted, and that is a deliberate, temporary shortcut, not
an oversight. It is safe only because every node is currently public, so
a forged identity grants nothing that isn't already world-readable. The
check in `require_trustworthy_identity` exists so that turning on private
mode without real auth fails loudly instead of silently exposing private
content to anyone who sets a header.
"""
from __future__ import annotations

from typing import Optional

from fastapi import Depends, Header, HTTPException, Request

from app.config import settings
from app.services.access import AccessScope
from app.services.governance import (
    BudgetExceeded,
    CostGovernor,
    RateLimiter,
    RateLimitExceeded,
)


async def get_scope(
    x_viewer_id: Optional[str] = Header(default=None),
) -> AccessScope:
    """
    The scope for this request. Anonymous by default — the normal case on
    a public commons, not a failure.
    """
    if x_viewer_id:
        return AccessScope.for_user(x_viewer_id)
    return AccessScope.anonymous()


def require_trustworthy_identity() -> None:
    """
    Guard against the dangerous combination: private content enabled
    while identity is still header-asserted.

    Called at startup rather than per-request so the failure is immediate
    and obvious, instead of surfacing as a quiet data leak in production.
    """
    if settings.private_visibility_enabled and not settings.real_auth_enabled:
        raise RuntimeError(
            "private_visibility_enabled is on, but real_auth_enabled is off. "
            "Viewer identity currently comes from an unverified X-Viewer-Id "
            "header, so anyone could read any private content by setting it. "
            "Build real authentication before enabling private visibility."
        )


def scope_key_for(scope: AccessScope, request: Request) -> str:
    """
    The key rate limits and budgets are counted against.

    Falls back to client IP for anonymous traffic — without it, every
    anonymous user shares one bucket, so a single script would exhaust
    the limit for everyone. Note this is the direct socket address: behind
    a proxy or load balancer that becomes the proxy's IP, collapsing all
    users into one bucket again. Real deployment needs a trusted
    X-Forwarded-For chain, which is deliberately not trusted here because
    an untrusted one is trivially spoofed to bypass limits entirely.
    """
    if scope.viewer_id:
        return f"viewer:{scope.viewer_id}"
    client = request.client.host if request.client else "unknown"
    return f"ip:{client}"


async def enforce_limits(
    request: Request,
    scope: AccessScope = Depends(get_scope),
) -> str:
    """
    Rate limit and budget check for LLM-spending endpoints.

    Returns the scope key so handlers can attribute spend to the same
    identity the limit was counted against.
    """
    if not settings.governance_enabled:
        return scope_key_for(scope, request)

    pool = request.app.state.pool
    key = scope_key_for(scope, request)
    endpoint = request.url.path

    try:
        await RateLimiter(pool).check_and_record(key, endpoint)
    except RateLimitExceeded as exc:
        raise HTTPException(
            429, str(exc), headers={"Retry-After": str(exc.retry_after_seconds)}
        ) from exc

    try:
        await CostGovernor(
            pool,
            daily_cap_usd=settings.daily_llm_budget_usd,
            per_viewer_daily_cap_usd=settings.per_viewer_daily_budget_usd,
        ).check_budget(key)
    except BudgetExceeded as exc:
        # 402 rather than 429: this is not a "slow down and retry" —
        # retrying immediately will fail identically until the window
        # rolls or the cap is raised.
        raise HTTPException(402, str(exc)) from exc

    return key


def make_cost_recorder(pool, scope_key: str, operation: str):
    """
    Build the `on_call` callback threaded through DebateEngine,
    Layer1Evaluator, SimulatedReplayEvaluator, DecompositionService, and
    ChatService (see their `on_call` parameters).

    This is the fix for a real gap: `CostGovernor.check_budget()` was
    being called and genuinely blocking requests, but nothing ever called
    `.record()`, so `llm_spend` stayed empty and the budget check always
    saw $0 spent regardless of real usage. The cap looked enforced and
    was not.

    Token counts are estimated from prompt/response text length
    (`estimate_tokens`), not read from provider usage fields -- General
    Compute and other OpenAI-compatible providers vary in whether and how
    they return exact usage, and a rough estimate that always fires beats
    an exact one that silently doesn't. Consistent with `estimated_cost`'s
    own documented purpose in the schema: a guardrail against runaway
    spend, not an accounting record.
    """
    from app.services.governance import CostGovernor, estimate_tokens

    governor = CostGovernor(pool)

    async def record(agent, prompt_text: str, response_text: str) -> None:
        try:
            await governor.record(
                provider=agent.family,
                model=agent.model_id,
                operation=operation,
                input_tokens=estimate_tokens(prompt_text),
                output_tokens=estimate_tokens(response_text),
                scope_key=scope_key,
            )
        except Exception as exc:  # noqa: BLE001
            # Recording failing must never break the actual response the
            # user is waiting on -- the LLM call already happened and was
            # already paid for. Logged, not raised.
            import logging
            logging.getLogger(__name__).error(
                "cost recording failed for %s/%s: %s", agent.family, agent.model_id, exc
            )

    return record
