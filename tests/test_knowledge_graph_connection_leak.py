"""Regression test for #6963: leaked SQLite handle in /api/memory/knowledge-graph.

`dashboard/app.py`'s `get_knowledge_graph` (the `/api/memory/knowledge-graph` and
`/api/knowledge-graph` handler) opened its brain.db connection with
`with sqlite3.connect(brain_db_path) as conn:`. That construct manages the
*transaction* (commit on success, rollback on exception) — sqlite3.Connection
has no `__exit__` that calls `close()`. So the handle was never closed on
either the success path or the exception path; it was only reclaimed
whenever the object happened to be garbage collected. This is the same
"unable to open database file" rot fixed for three sibling endpoints in
#6954 (see tests/test_dashboard_sqlite_leak_fix.py) — #6954's commit
explicitly called out this site as the one that still needed it, since
`with conn:` looks like a fix but isn't.

This test forces both a successful request and a failing query through the
endpoint and asserts the underlying sqlite3.Connection.close() actually
runs in both cases (via a sqlite3.Connection subclass passed as `factory=`,
since Connection instances don't allow attribute monkeypatching).
"""

import asyncio
import sqlite3
import threading
from pathlib import Path

import pytest
from starlette.requests import Request

import dashboard.app as dashboard_app
import dashboard.routes.memory as memory_routes  # noqa: E402


def _run_coro(coro):
    """Run `coro` to completion on a brand-new event loop in a dedicated
    OS thread, and return its result (re-raising any exception).

    Neither `asyncio.run()` on the main thread (closes the loop other
    tests' `get_event_loop()` calls expect open — see
    test_dashboard_sqlite_leak_fix.py) nor
    `asyncio.get_event_loop_policy().get_event_loop().run_until_complete()`
    (collides with the main thread's asyncio state once
    tests/test_kanban_visual.py has run Playwright's sync API, which
    drives its own event loop via greenlets on that same thread and
    leaves `asyncio.events._get_running_loop()` non-None — confirmed with
    a minimal repro independent of this endpoint) is safe here. A fresh
    thread has never touched that state, so `asyncio.run()` there is
    unaffected by ordering relative to other test files.
    """
    box: dict = {}

    def _runner():
        try:
            box["result"] = asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller's thread
            box["error"] = exc

    thread = threading.Thread(target=_runner)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box["result"]


class _SpyConnection(sqlite3.Connection):
    """sqlite3.Connection subclass that records close() calls."""

    close_calls = 0

    def close(self):
        type(self).close_calls += 1
        super().close()


_real_connect = sqlite3.connect


def _make_connect(db_path: Path, fail: bool = False):
    """Build a sqlite3.connect replacement that always opens db_path via
    _SpyConnection, regardless of the path/args the caller passed in.
    Optionally makes the first query raise.
    """

    def _connect(*args, **_kwargs):
        _connect.requested.append(args[0] if args else None)
        conn = _real_connect(db_path, factory=_SpyConnection)
        if fail:
            def _boom(*_a, **_k):
                raise sqlite3.OperationalError("simulated query failure")

            conn.execute = _boom
        return conn

    _connect.requested = []
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
        "CREATE TABLE graph_nodes (id INTEGER PRIMARY KEY, type TEXT, name TEXT, "
        "description TEXT, mention_count INTEGER)"
    )
    setup_conn.execute(
        "CREATE TABLE graph_edges (source_node_id INTEGER, target_node_id INTEGER, "
        "type TEXT, weight REAL)"
    )
    setup_conn.execute(
        "INSERT INTO graph_nodes VALUES (1, 'concept', 'a', 'desc', 5)"
    )
    setup_conn.execute(
        "INSERT INTO graph_nodes VALUES (2, 'concept', 'b', 'desc', 3)"
    )
    setup_conn.execute(
        "INSERT INTO graph_edges VALUES (1, 2, 'relates_to', 1.0)"
    )
    setup_conn.commit()
    setup_conn.close()
    return db_path


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/memory/knowledge-graph",
            "headers": [],
            "client": ("testclient", 5000),
            "server": ("testserver", 80),
            "scheme": "http",
            "query_string": b"",
        }
    )


def _force_brain_db_present(monkeypatch: pytest.MonkeyPatch, db_path: Path, fail: bool):
    """Make the spy connect open the fixture's brain.db (the configured memory store's).

    The `brain_db` fixture requests `memory_store`, so the endpoint already
    finds that file; no global Path.exists patch required. Returns the spy,
    whose .requested lists the paths the endpoint asked to open.
    """
    spy = _make_connect(db_path, fail=fail)
    monkeypatch.setattr(sqlite3, "connect", spy)
    return spy


def test_knowledge_graph_closes_connection_on_success(monkeypatch, brain_db):
    spy = _force_brain_db_present(monkeypatch, brain_db, fail=False)

    result = _run_coro(memory_routes.get_knowledge_graph(_request()))

    assert _SpyConnection.close_calls == 1
    assert result["stats"]["total_nodes"] == 2
    assert [Path(p) for p in spy.requested] == [brain_db]  # the configured store, nothing else


def test_knowledge_graph_closes_connection_on_query_failure(monkeypatch, brain_db):
    _force_brain_db_present(monkeypatch, brain_db, fail=True)

    result = _run_coro(memory_routes.get_knowledge_graph(_request()))

    assert _SpyConnection.close_calls == 1
    assert result.status_code == 500
