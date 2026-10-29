"""Regression tests for #6954: leaked SQLite handles on the error path.

`dashboard/app.py` had three brain.db call sites that did
`conn = sqlite3.connect(...)` inside a bare `try:` and called `conn.close()`
only on the happy path. Any exception raised between open and close (a bad
query, a locked file, a corrupt row) skipped the close and leaked the file
handle — the same "unable to open database file" rot documented in
test_dashboard_health.py, just from a different angle (handle exhaustion
instead of a single rotted handle).

These tests force a query failure inside each of the three fixed endpoints
and assert the underlying sqlite3.Connection.close() still runs.
"""

import asyncio
import sqlite3
from pathlib import Path

import pytest

import dashboard.app as dashboard_app


class _SpyConnection(sqlite3.Connection):
    """sqlite3.Connection subclass that records close() calls.

    sqlite3.Connection instances don't allow arbitrary attribute
    assignment (no __dict__), so spying requires a real subclass passed
    via the `factory=` argument rather than monkeypatching an instance.
    """

    close_calls = 0

    def close(self):
        type(self).close_calls += 1
        super().close()


_real_connect = sqlite3.connect


def _make_failing_connect(db_path: Path):
    """Build a sqlite3.connect replacement that always opens db_path via
    _SpyConnection and fails the first query, regardless of the path/args
    the caller passed in.
    """

    def _connect(*_args, **_kwargs):
        conn = _real_connect(db_path, factory=_SpyConnection)

        def _boom(*_a, **_k):
            raise sqlite3.OperationalError("simulated query failure")

        conn.execute = _boom
        return conn

    return _connect


@pytest.fixture(autouse=True)
def _reset_spy():
    _SpyConnection.close_calls = 0
    yield
    _SpyConnection.close_calls = 0


@pytest.fixture
def brain_db(memory_store) -> Path:
    db_path = memory_store.root / "brain.db"
    setup_conn = sqlite3.connect(db_path)
    setup_conn.execute(
        "CREATE TABLE thoughts (id INTEGER PRIMARY KEY, content TEXT, "
        "embedding TEXT, embedding_blob BLOB, metadata TEXT, created_at TEXT, source_machine TEXT)"
    )
    setup_conn.commit()
    setup_conn.close()
    return db_path


def _force_brain_db_present(monkeypatch: pytest.MonkeyPatch, db_path: Path):
    """brain_db_path.exists() must be True for the try block to be reached.

    The real path is derived from `Path(__file__).parent.parent.parent`,
    which does not point at a real memory store checkout inside a worktree, so
    the endpoints would otherwise short-circuit on the 404 branch before
    ever calling sqlite3.connect.
    """
    monkeypatch.setattr(Path, "exists", lambda self: True)
    monkeypatch.setattr(sqlite3, "connect", _make_failing_connect(db_path))


@pytest.mark.parametrize(
    "endpoint, check_result",
    [
        ("get_memory_graph_data", lambda r: r.status_code == 500),
        # Falls back to the canonical defaults when the query fails.
        (
            "get_memory_types",
            lambda r: r == {"types": ["observation", "decision", "idea", "question"]},
        ),
        ("get_memory_heatmap", lambda r: r.status_code == 500),
    ],
    ids=["memory_graph", "memory_types", "memory_heatmap"],
)
def test_memory_endpoint_closes_connection_on_query_failure(
    monkeypatch, brain_db, endpoint, check_result
):
    # Bypass the module-level graph cache so the failing query actually runs.
    dashboard_app._graph_cache["data"] = None
    dashboard_app._graph_cache["timestamp"] = 0
    dashboard_app._graph_cache["params"] = None

    _force_brain_db_present(monkeypatch, brain_db)

    # run_until_complete on the existing loop, NOT asyncio.run() — the latter
    # closes the loop on exit, and test_github_api.py reaches for
    # get_event_loop() afterwards and gets a closed one (see
    # test_dashboard_blocked_resolution.py for the same note).
    loop = asyncio.get_event_loop_policy().get_event_loop()
    result = loop.run_until_complete(getattr(dashboard_app, endpoint)())

    assert _SpyConnection.close_calls == 1
    assert check_result(result)
