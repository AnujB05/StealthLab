"""
Access-scope tests (V2).

The predicate builder is pure, so it's tested directly. The leak cases
that matter -- traversal walking *through* private edges, citation
verification confirming invisible nodes -- are tested against real
Postgres in integration_check_v2.py, because they're properties of the
SQL, not of the Python.
"""
from __future__ import annotations

from app.services.access import AccessScope, next_param_index, visibility_predicate


def test_anonymous_scope_sees_public_only():
    sql, params = visibility_predicate(AccessScope.anonymous())
    assert sql == "visibility = 'public'"
    assert params == []


def test_user_scope_sees_public_plus_own():
    sql, params = visibility_predicate(AccessScope.for_user("alice"), param_index=3)
    assert "visibility = 'public'" in sql
    assert "owner_id = $3" in sql
    assert params == ["alice"]


def test_unrestricted_scope_returns_literal_true_not_empty():
    """
    An empty predicate would be AND-ed into a WHERE clause and silently
    vanish, which is the exact failure this module exists to prevent.
    TRUE is visibly permissive in the query text.
    """
    sql, params = visibility_predicate(AccessScope.unrestricted())
    assert sql == "TRUE"
    assert params == []


def test_predicate_is_never_empty_for_any_scope():
    """No scope may produce a droppable filter."""
    for scope in (
        AccessScope.anonymous(),
        AccessScope.for_user("bob"),
        AccessScope.unrestricted(),
        AccessScope(viewer_id="c", include_private=False),
    ):
        sql, _ = visibility_predicate(scope)
        assert sql.strip(), f"empty predicate for {scope}"


def test_alias_prefixes_every_column_reference():
    """
    In a joined query an unqualified column is ambiguous and errors at
    runtime -- so the alias must reach *both* sides of the OR.
    """
    sql, _ = visibility_predicate(AccessScope.for_user("alice"), alias="e", param_index=2)
    assert "e.visibility" in sql
    assert "e.owner_id" in sql
    assert " visibility" not in sql.replace("e.visibility", "")


def test_param_index_is_honoured():
    """Wrong placeholder numbering binds the wrong values -- silently."""
    for index in (1, 4, 9):
        sql, params = visibility_predicate(AccessScope.for_user("x"), param_index=index)
        assert f"${index}" in sql
        assert len(params) == 1


def test_next_param_index_tracks_consumption():
    assert next_param_index(AccessScope.for_user("alice"), 1) == 2
    assert next_param_index(AccessScope.anonymous(), 1) == 1
    assert next_param_index(AccessScope.unrestricted(), 1) == 1


def test_user_scope_without_private_falls_back_to_public():
    sql, params = visibility_predicate(
        AccessScope(viewer_id="alice", include_private=False)
    )
    assert sql == "visibility = 'public'"
    assert params == []


def test_startup_guard_blocks_private_content_without_real_auth():
    """
    The dangerous combination: private visibility on, identity still an
    unverified header. Must fail loudly at startup, not leak quietly.
    """
    import pytest

    from app.api.deps import require_trustworthy_identity
    from unittest.mock import patch

    with patch("app.api.deps.settings") as s:
        s.private_visibility_enabled = True
        s.real_auth_enabled = False
        with pytest.raises(RuntimeError, match="private_visibility_enabled"):
            require_trustworthy_identity()

    # Safe combinations must pass.
    for private, auth in ((False, False), (False, True), (True, True)):
        with patch("app.api.deps.settings") as s:
            s.private_visibility_enabled = private
            s.real_auth_enabled = auth
            require_trustworthy_identity()
