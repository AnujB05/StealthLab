"""
Access scoping (V2).

One module builds every visibility predicate in the system. Nothing
outside this file should write `visibility = ...` or `owner_id = ...`
into a query.

That constraint is the entire point. V0 put a `tenant_id` column on
every table and then never filtered by it anywhere — so isolation
looked implemented and was decorative, and turning it on later would
have meant auditing every query in the codebase. Centralising the
predicate here means V2's private mode ships as a change to one
function, verified by one set of tests, rather than a codebase-wide
audit hoping nothing was missed.

Current policy: a fully shared commons. Everything is public, so the
predicate is permissive — but it is *present in every query path*, which
is what makes flipping it cheap.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class AccessScope:
    """
    Who is asking, and what they may therefore see.

    `viewer_id` is None for anonymous public traffic — the default on a
    public commons, not an error condition.
    """

    viewer_id: Optional[str] = None
    include_private: bool = True

    @classmethod
    def anonymous(cls) -> "AccessScope":
        return cls(viewer_id=None)

    @classmethod
    def for_user(cls, viewer_id: str) -> "AccessScope":
        return cls(viewer_id=viewer_id)

    @classmethod
    def unrestricted(cls) -> "AccessScope":
        """
        Bypasses visibility entirely. For internal maintenance paths
        (backfills, migrations, admin tooling) — never for a request
        originating from a user.
        """
        return cls(viewer_id=None, include_private=False)

    @property
    def is_unrestricted(self) -> bool:
        return self.viewer_id is None and not self.include_private


def visibility_predicate(
    scope: AccessScope, alias: str = "", param_index: int = 1
) -> tuple[str, list]:
    """
    Build a SQL predicate and its parameters for the given scope.

    Returns (sql_fragment, params). The fragment is always a complete
    boolean expression safe to AND into a WHERE clause — never an empty
    string, because an empty string silently drops the filter and that is
    exactly the failure this module exists to prevent. An unrestricted
    scope returns the literal `TRUE`, which is visibly permissive in the
    query text rather than invisibly absent.

    `param_index` is the first positional placeholder available to the
    caller ($1, $2, ...). asyncpg has no named parameters, so callers
    must thread this correctly; get it wrong and the query binds the
    wrong values. `next_param_index` below makes that explicit.
    """
    prefix = f"{alias}." if alias else ""

    if scope.is_unrestricted:
        return "TRUE", []

    if scope.viewer_id is None:
        # Anonymous: public content only.
        return f"{prefix}visibility = 'public'", []

    if not scope.include_private:
        return f"{prefix}visibility = 'public'", []

    # Signed in: public content, plus anything they own.
    return (
        f"({prefix}visibility = 'public' OR {prefix}owner_id = ${param_index})",
        [scope.viewer_id],
    )


def next_param_index(scope: AccessScope, current: int) -> int:
    """
    How many placeholders `visibility_predicate` consumed, so the caller
    knows where its own parameters resume. Threading this by hand is a
    real source of off-by-one bugs in raw SQL.
    """
    _, params = visibility_predicate(scope, param_index=current)
    return current + len(params)
