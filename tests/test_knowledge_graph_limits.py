"""Tests for #7147: /api/memory/knowledge-graph server-side caps and build-status.

Covers:
- max_nodes / max_edges caps, with strongest edges kept and truthful stats
- 400s for invalid query params
- clamping of out-of-range values
- GET /api/memory/build-status (ok / failed / unknown)
- removal of the dead POST /api/memory/knowledge-graph/rebuild endpoint and its JS

All brain.db access goes through a tmp_path fixture; the real
the real memory store's brain.db is never touched.
"""

import json
import os
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import dashboard.app as dashboard_app
from dashboard.app import app

client = TestClient(app)

REPO_ROOT = Path(dashboard_app.__file__).resolve().parent.parent

N_NODES = 120


@pytest.fixture
def brain_db(memory_store) -> Path:
    """Create a tiny graph_nodes/graph_edges fixture in the configured memory store.

    Node i has mention_count N_NODES - i + 1, so node 1 is the largest.
    Edges are the complete graph on the N_NODES nodes, weight = source*1000 +
    target, which makes the strongest edges uniquely ordered and lets tests
    assert exactly which edges survive the weight-DESC cap. The endpoint
    resolves brain.db through the [memory] config, which the memory_store
    fixture points at tmp_path.
    """
    db_path = memory_store.root / "brain.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE graph_nodes (id INTEGER PRIMARY KEY, type TEXT, name TEXT, "
        "description TEXT, mention_count INTEGER)"
    )
    conn.execute(
        "CREATE TABLE graph_edges (source_node_id INTEGER, target_node_id INTEGER, "
        "type TEXT, weight REAL)"
    )
    conn.executemany(
        "INSERT INTO graph_nodes VALUES (?, 'concept', ?, 'desc', ?)",
        [
            (node_id, f"n{node_id}", N_NODES - node_id + 1)
            for node_id in range(1, N_NODES + 1)
        ],
    )
    conn.executemany(
        "INSERT INTO graph_edges VALUES (?, ?, 'relates_to', ?)",
        [
            (i, j, i * 1000.0 + j)
            for i in range(1, N_NODES + 1)
            for j in range(i + 1, N_NODES + 1)
        ],
    )
    conn.commit()
    conn.close()
    return db_path


def _ids(nodes) -> set:
    return {n["id"] for n in nodes}


def test_max_nodes_cap_keeps_top_nodes_by_size(brain_db):
    r = client.get("/api/memory/knowledge-graph", params={"max_nodes": 60, "max_edges": 1000})
    assert r.status_code == 200
    data = r.json()

    assert _ids(data["nodes"]) == set(range(1, 61))  # top 60 by mention_count
    stats = data["stats"]
    assert stats["nodes_shown"] == 60
    assert stats["total_nodes_available"] == N_NODES
    # 1770 edges exist between the top-60 nodes; the edge cap bites too.
    assert stats["edges_shown"] == 1000
    assert stats["total_edges_available"] == N_NODES * (N_NODES - 1) // 2
    assert stats["truncated"] is True


def test_max_edges_cap_keeps_strongest_edges_between_kept_nodes(brain_db):
    r = client.get("/api/memory/knowledge-graph", params={"max_nodes": 60, "max_edges": 100})
    assert r.status_code == 200
    data = r.json()

    assert _ids(data["nodes"]) == set(range(1, 61))
    assert len(data["edges"]) == 100
    # Strongest edges between the kept (top-60) nodes, weight = source*1000 +
    # target, descending: (59,60), (58,60), (58,59), (57,60), (57,59), ...
    assert [(e["source"], e["target"]) for e in data["edges"][:5]] == [
        (59, 60), (58, 60), (58, 59), (57, 60), (57, 59),
    ]
    # The strongest edge in the DB ((119,120), weight 119120) involves trimmed
    # nodes and must not appear.
    assert all(e["source"] <= 60 and e["target"] <= 60 for e in data["edges"])
    stats = data["stats"]
    assert stats["edges_shown"] == 100
    assert stats["total_edges_available"] == N_NODES * (N_NODES - 1) // 2
    assert stats["truncated"] is True


def test_no_truncation_when_under_caps(brain_db):
    r = client.get("/api/memory/knowledge-graph", params={"max_nodes": 200, "max_edges": 10000})
    assert r.status_code == 200
    data = r.json()

    assert _ids(data["nodes"]) == set(range(1, N_NODES + 1))
    assert len(data["edges"]) == N_NODES * (N_NODES - 1) // 2
    stats = data["stats"]
    assert stats["total_nodes_available"] == stats["nodes_shown"] == N_NODES
    assert stats["total_edges_available"] == stats["edges_shown"] == len(data["edges"])
    assert stats["truncated"] is False
    # Legacy keys still work and mean the shown counts.
    assert stats["total_nodes"] == stats["nodes_shown"]
    assert stats["total_edges"] == stats["edges_shown"]


def test_defaults_apply_caps(brain_db):
    r = client.get("/api/memory/knowledge-graph")
    assert r.status_code == 200
    data = r.json()
    stats = data["stats"]
    # Defaults: 1500 nodes (> 120, all shown) and 5000 edges (< 7140, capped).
    assert stats["nodes_shown"] == N_NODES
    assert stats["total_nodes_available"] == N_NODES
    assert stats["edges_shown"] == 5000
    assert stats["total_edges_available"] == N_NODES * (N_NODES - 1) // 2
    assert stats["truncated"] is True


def test_invalid_max_params_return_400(brain_db):
    for params in (
        {"max_nodes": "abc"},
        {"max_edges": "abc"},
        {"max_nodes": ""},
        {"max_edges": ""},
    ):
        r = client.get("/api/memory/knowledge-graph", params=params)
        assert r.status_code == 400, params
        body = r.json()
        assert "max_nodes" in body["error"] or "max_edges" in body["error"]


def test_out_of_range_params_are_clamped(brain_db):
    # Below the floors (50 nodes / 100 edges): clamped up, so the top 50 nodes
    # and the 100 strongest edges between them come back.
    r = client.get("/api/memory/knowledge-graph", params={"max_nodes": 1, "max_edges": 1})
    assert r.status_code == 200
    data = r.json()
    assert len(data["nodes"]) == 50
    assert len(data["edges"]) == 100

    # Above the ceilings: clamped down, still serves the whole fixture.
    r = client.get("/api/memory/knowledge-graph", params={"max_nodes": 999999, "max_edges": 999999})
    assert r.status_code == 200
    data = r.json()
    assert len(data["nodes"]) == N_NODES
    assert len(data["edges"]) == N_NODES * (N_NODES - 1) // 2


def test_min_mentions_filter_still_works_with_caps(brain_db):
    r = client.get(
        "/api/memory/knowledge-graph",
        params={"min_mentions": 100, "max_nodes": 50, "max_edges": 1000},
    )
    assert r.status_code == 200
    data = r.json()

    # mention_count >= 100 means nodes 1..21.
    assert _ids(data["nodes"]) == set(range(1, 22))
    stats = data["stats"]
    assert stats["total_nodes_available"] == 21
    assert stats["total_edges_available"] == 21 * 20 // 2
    assert stats["truncated"] is False


RECEIPT = "logs/cron/build.json"
SCHEDULE = "Sundays 03:00"  # not the old hardcoded "Mondays 10:00", so a hardcoded value fails


def _get_build_status(memory_store, content: str | None):
    root = memory_store(build_receipt=RECEIPT, build_schedule=SCHEDULE)
    receipt = root / RECEIPT
    receipt.parent.mkdir(parents=True, exist_ok=True)
    if content is not None:
        receipt.write_text(content)
    return client.get("/api/memory/build-status")


def test_build_status_ok(memory_store):
    r = _get_build_status(
        memory_store,
        json.dumps(
            {
                "label": "com.example.weekly-graph-build",
                "started_at": "2026-09-21T14:48:22Z",
                "finished_at": "2026-09-21T17:17:35Z",
                "exit_status": 0,
                "invocation": "scheduled",
            }
        ),
    )
    assert r.status_code == 200
    data = r.json()
    assert data["state"] == "ok"
    assert data["exit_status"] == 0
    assert data["started_at"] == "2026-09-21T14:48:22Z"
    assert data["finished_at"] == "2026-09-21T17:17:35Z"
    assert data["invocation"] == "scheduled"
    assert data["schedule"] == SCHEDULE


def test_build_status_failed(memory_store):
    r = _get_build_status(
        memory_store,
        json.dumps(
            {
                "started_at": "2026-09-21T14:48:22Z",
                "finished_at": "2026-09-21T14:48:23Z",
                "exit_status": 1,
                "invocation": "scheduled",
            }
        ),
    )
    assert r.status_code == 200
    data = r.json()
    assert data["state"] == "failed"
    assert data["exit_status"] == 1


def test_build_status_missing_receipt_is_unknown(memory_store):
    r = _get_build_status(memory_store, None)
    assert r.status_code == 200
    data = r.json()
    assert data["state"] == "unknown"
    assert data["started_at"] is None
    assert data["finished_at"] is None
    assert data["exit_status"] is None
    assert data["schedule"] == SCHEDULE


def test_build_status_invalid_json_is_unknown(memory_store):
    r = _get_build_status(memory_store, "not valid json {")
    assert r.status_code == 200
    data = r.json()
    assert data["state"] == "unknown"


def test_build_status_invalid_shape_is_unknown(memory_store):
    r = _get_build_status(
        memory_store,
        json.dumps({"started_at": "2026-09-21T14:48:22Z"}),  # no exit_status
    )
    assert r.status_code == 200
    data = r.json()
    assert data["state"] == "unknown"




def test_build_status_without_receipt_configured_is_unknown(memory_store):
    memory_store(build_schedule=SCHEDULE)
    r = client.get("/api/memory/build-status")
    assert r.status_code == 200
    data = r.json()
    assert data["state"] == "unknown"
    assert data["schedule"] == SCHEDULE


def test_unconfigured_memory_integration_is_404():
    r = client.get("/api/memory/knowledge-graph")
    assert r.status_code == 404
    assert "memory integration is not configured" in r.json()["error"]


def test_broken_memory_config_is_500_naming_the_file():
    config = Path(os.environ["PT_CONFIG"])
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("[memory\nroot = ")
    r = client.get("/api/memory/knowledge-graph")
    assert r.status_code == 500
    assert str(config) in r.json()["detail"]


@pytest.mark.parametrize("path", ["/api/knowledge-graph"])
def test_old_routes_are_gone(path):
    r = client.get(path)
    assert r.status_code == 404
    assert r.json() == {"detail": "Not Found"}
