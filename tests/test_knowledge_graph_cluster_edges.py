"""Tests for #7657 (follow-up to #7147): clustered-edge collapsing in
GET /api/memory/knowledge-graph with cluster=on.

When dust nodes collapse into a per-type cluster node, two distinct things
can happen to the edges between them:

1. Multiple original edges can remap onto the SAME (cluster, cluster, type)
   key. _KNOWLEDGE_EDGE_SELECT orders by weight DESC and the app-level dedup
   in get_knowledge_graph keeps only the first (strongest) survivor per key.
2. An edge between two dust nodes of the SAME type collapses onto a
   self-loop (source cluster == target cluster) and is dropped entirely by
   the `!=` guard in _KNOWLEDGE_EDGE_FROM — it never reaches the dedup step
   or the response.

This fixture builds two dust clusters (3 "concept" nodes, 3 "task" nodes,
all mention_count <= 2 and degree <= 5) with:
  - one intra-concept edge (1 -> 2) that collapses to a concept/concept
    self-loop, which must be dropped,
  - three concept -> task edges (1->4, 2->5, 3->6) that all collapse to the
    same (concept_cluster, task_cluster, 'relates_to') key, of which only
    the strongest (2->5, weight 200) must survive.

All brain.db access goes through a tmp_path fixture; the real
the real memory store's brain.db is never touched.
"""

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dashboard.app import app

client = TestClient(app)


@pytest.fixture
def cluster_brain_db(memory_store) -> Path:
    """Two dust clusters (concept x3, task x3) wired for collapse testing.

    Node ids 1-3 are type 'concept', ids 4-6 are type 'task'. All have
    mention_count=1 (<= 2) and end up with degree <= 5, so every node is
    "dust" per _cluster_dust_nodes and each type group (size 3) is large
    enough to actually cluster (clustering skips groups smaller than 3).

    Edges:
      (1, 2, weight=50)  -> both concept -> same cluster -> self-loop, dropped
      (1, 4, weight=100) -> concept cluster -> task cluster
      (2, 5, weight=200) -> concept cluster -> task cluster (strongest)
      (3, 6, weight=150) -> concept cluster -> task cluster
    The last three all remap onto the same (concept_cluster, task_cluster,
    'relates_to') key; only weight=200 should survive.
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
        "INSERT INTO graph_nodes VALUES (?, ?, ?, 'desc', 1)",
        [
            (1, "concept", "c1"), (2, "concept", "c2"), (3, "concept", "c3"),
            (4, "task", "t1"), (5, "task", "t2"), (6, "task", "t3"),
        ],
    )
    conn.executemany(
        "INSERT INTO graph_edges VALUES (?, ?, 'relates_to', ?)",
        [
            (1, 2, 50.0),
            (1, 4, 100.0),
            (2, 5, 200.0),
            (3, 6, 150.0),
        ],
    )
    conn.commit()
    conn.close()
    return db_path


def test_cluster_on_collapses_edges_truthfully(cluster_brain_db):
    r = client.get("/api/memory/knowledge-graph", params={"cluster": "on"})
    assert r.status_code == 200
    data = r.json()

    nodes = data["nodes"]
    assert len(nodes) == 2
    assert all(n["is_cluster"] for n in nodes)
    concept_cluster = next(n for n in nodes if n["type"] == "concept")
    task_cluster = next(n for n in nodes if n["type"] == "task")
    assert concept_cluster["cluster_count"] == 3
    assert task_cluster["cluster_count"] == 3

    edges = data["edges"]

    # The strongest edge survives per collapsed key: of the three raw edges
    # that all remap onto (concept_cluster, task_cluster, 'relates_to'),
    # only the weight=200 one (originally 2 -> 5) comes back.
    assert len(edges) == 1
    survivor = edges[0]
    assert survivor["weight"] == 200.0
    assert {survivor["source"], survivor["target"]} == {
        concept_cluster["id"], task_cluster["id"],
    }

    # Collapsed self-loops are dropped: the (1, 2, weight=50) edge remaps to
    # (concept_cluster, concept_cluster) and must not appear in any form.
    assert all(e["source"] != e["target"] for e in edges)
    assert not any(e["weight"] == 50.0 for e in edges)

    # Stats stay truthful: nothing here exceeds the (generous) default caps,
    # so nothing is truncated, and the "available" counts match what was
    # actually computed post-clustering/post-dedup, not the raw DB counts
    # (4 raw edges, 6 raw nodes) that would overstate what's really there.
    stats = data["stats"]
    assert stats["clustered"] is True
    assert stats["nodes_shown"] == stats["total_nodes_available"] == 2
    assert stats["edges_shown"] == stats["total_edges_available"] == 1
    assert stats["truncated"] is False
