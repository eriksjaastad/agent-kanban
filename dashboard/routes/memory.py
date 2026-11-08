"""The memory and project-graph API: graph data, knowledge graph, build status, types and heatmap, plus tracked rebuilds."""

import json
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime
from pathlib import Path
from time import time as _time
from typing import Dict, Optional

import numpy as np
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from dashboard.common import (
    _memory_off_message,
    _memory_store,
    build_template_context,
    logger,
    templates,
)

router = APIRouter()


_graph_cache: Dict = {"data": None, "timestamp": 0, "params": None}


_GRAPH_CACHE_TTL = 300  # 5 minutes


def _invalidate_graph_cache() -> None:
    """Drop the /api/memory-graph in-memory cache so the next request recomputes."""
    _graph_cache["data"] = None
    _graph_cache["timestamp"] = 0
    _graph_cache["params"] = None


# ---- Rebuild job tracker -------------------------------------------------
# Tracks in-flight rebuild jobs so the frontend can poll real completion
# status instead of guessing via node-count diffs. Job dicts live for the
# lifetime of the process; they're small and self-expire on restart.
_rebuild_jobs: Dict[str, Dict] = {}


def _run_tracked_rebuild(job_id: str, fn) -> None:
    """Run a rebuild function with status tracking. Invalidates graph cache on success."""
    job = _rebuild_jobs.get(job_id)
    if job is None:
        return
    job["status"] = "running"
    job["started_at"] = datetime.now().isoformat()
    try:
        fn()
        job["status"] = "done"
        job["error"] = None
        _invalidate_graph_cache()
    except Exception as e:
        logger.error(f"Rebuild job {job_id} ({job.get('kind')}) failed: {e}")
        job["status"] = "error"
        job["error"] = str(e)
    finally:
        job["finished_at"] = datetime.now().isoformat()


def _create_rebuild_job(kind: str) -> str:
    job_id = uuid.uuid4().hex[:12]
    _rebuild_jobs[job_id] = {
        "job_id": job_id,
        "kind": kind,
        "status": "queued",
        "started_at": None,
        "finished_at": None,
        "error": None,
    }
    return job_id


@router.get("/api/rebuild-status/{job_id}")
async def get_rebuild_status(job_id: str):
    """Poll status of a rebuild job. Returns 404 if unknown job."""
    job = _rebuild_jobs.get(job_id)
    if job is None:
        return JSONResponse({"error": "unknown job_id"}, status_code=404)
    return job


@router.get("/graph", response_class=HTMLResponse)
async def graph_view(request: Request):
    """Render the graph visualization page."""
    return templates.TemplateResponse(request, "graph.html", build_template_context(request))


@router.get("/memory", response_class=HTMLResponse)
async def memory_view(request: Request):
    """Render the memory graph visualization page."""
    return templates.TemplateResponse(request, "memory.html", build_template_context(request))


@router.get("/api/memory-graph")
async def get_memory_graph_data(
    min_similarity: float = 0.3,   # Minimum similarity threshold for edges
    max_edges_per_node: int = 10,  # Limit edges per node to keep response size reasonable
    max_nodes: int = 1000          # Cap nodes to prevent N×N memory explosion
):
    """Return full memory graph JSON for client-side rendering.

    Reads the configured memory store's `thoughts` table (content, metadata,
    embedding_blob); used by memory.js (canvas) and memory_svg.js (SVG).

    Type filtering is intentionally done client-side so the browser can
    switch filters instantly without a network round-trip.

    Uses numpy vectorized cosine similarity for ~1600x speedup over
    pure Python loops on large graphs.
    """
    # The store is resolved before the cache, and is part of its key, so a
    # config change takes effect on the next request rather than in 5 minutes.
    store = _memory_store()
    if store is None:
        return JSONResponse({"error": _memory_off_message()}, status_code=404)
    brain_db_path = store.brain_db

    now = _time()
    cache_key = (str(brain_db_path), min_similarity, max_edges_per_node, max_nodes)
    if (_graph_cache["data"] is not None
            and now - _graph_cache["timestamp"] < _GRAPH_CACHE_TTL
            and _graph_cache["params"] == cache_key):
        return _graph_cache["data"]

    if not brain_db_path.exists():
        return JSONResponse({
            "error": f"Memory database not found. Expected at: {brain_db_path}"
        }, status_code=404)

    try:
        with closing(sqlite3.connect(brain_db_path)) as conn:
            conn.row_factory = sqlite3.Row

            rows = conn.execute(
                # The memory store keeps embeddings only as little-endian float32
                # blobs (its #8035); the JSON `embedding` column is empty. A
                # value that is not a BLOB holding a whole float32 vector is
                # skipped here, before LIMIT, as the memory store's unpack_embedding
                # drops it.
                "SELECT id, content, embedding_blob, metadata, created_at, source_machine FROM thoughts"
                " WHERE typeof(embedding_blob) = 'blob'"
                " AND length(embedding_blob) > 0 AND length(embedding_blob) % 4 = 0"
                " ORDER BY created_at DESC LIMIT ?",
                (max_nodes,)
            ).fetchall()
            total_in_db = conn.execute("SELECT COUNT(*) FROM thoughts").fetchone()[0]

        nodes = []
        embeddings = []

        for row in rows:
            metadata = json.loads(row["metadata"]) if row["metadata"] else {}
            embedding = np.frombuffer(row["embedding_blob"], dtype="<f4")
            thought_type_val = metadata.get("type", "observation")

            nodes.append({
                "id": str(row["id"]),
                "content": row["content"],
                "type": thought_type_val,
                "project": metadata.get("project", ""),
                "agent_family": metadata.get("agent_family", ""),
                "created_at": row["created_at"],
                "source_machine": row["source_machine"] or "",
                "size": 1
            })
            embeddings.append(embedding)

        # Vectorized cosine similarity via numpy
        edges = []
        connection_counts = {node["id"]: 0 for node in nodes}

        if embeddings:
            matrix = np.array(embeddings, dtype=np.float32)
            # Drop raw Python lists immediately — numpy has its own copy
            del embeddings
            norms = np.linalg.norm(matrix, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            normed = matrix / norms
            del matrix, norms  # free before allocating N×N
            sim_matrix = normed @ normed.T
            del normed  # free N×D before iterating N×N
            np.fill_diagonal(sim_matrix, 0)

            # Edge extraction with threshold + top-k per node
            for i in range(len(nodes)):
                row_slice = sim_matrix[i, i+1:]
                mask = row_slice > min_similarity
                indices = np.where(mask)[0]
                values = row_slice[mask]
                if len(values) > max_edges_per_node:
                    top_k = np.argpartition(values, -max_edges_per_node)[-max_edges_per_node:]
                    indices = indices[top_k]
                    values = values[top_k]
                for idx, sim_val in zip(indices, values):
                    j = int(idx) + i + 1
                    edges.append({
                        "source": nodes[i]["id"],
                        "target": nodes[j]["id"],
                        "similarity": round(float(sim_val), 3)
                    })
                    connection_counts[nodes[i]["id"]] += 1
                    connection_counts[nodes[j]["id"]] += 1
            del sim_matrix

        for node in nodes:
            node["size"] = connection_counts[node["id"]]

        result = {
            "generated_at": datetime.now().isoformat(),
            "stats": {
                "total_thoughts": len(nodes),
                "total_in_db": total_in_db,
                "total_connections": len(edges),
                "avg_connections": round(sum(connection_counts.values()) / len(nodes), 2) if nodes else 0
            },
            "nodes": nodes,
            "edges": edges
        }

        _graph_cache["data"] = result
        _graph_cache["timestamp"] = _time()
        _graph_cache["params"] = cache_key

        return result

    except Exception as e:
        logger.error(f"Error reading memory database: {e}")
        return JSONResponse({"error": f"Error reading memory data: {e}"}, status_code=500)


def _parse_bounded_int_param(request: Request, name: str, default: int, lo: int, hi: int):
    """Parse an integer query param with a default, clamped to [lo, hi].

    Returns (value, error_message). A non-integer value yields an error
    message so the caller can return a 400 instead of silently falling back
    to the default. Out-of-range integer values are clamped into the
    documented range.
    """
    raw = request.query_params.get(name)
    if raw is None:
        return default, None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None, f"{name} must be an integer (got {raw!r})"
    return max(lo, min(hi, value)), None


# Final edge query shared by the shown-edge select and the available-edge
# count. Endpoints are remapped through _cluster_map (dust -> cluster) and
# then required to land inside _kept_ids (populated with either the shown
# node ids or the available node ids before the cap).
_KNOWLEDGE_EDGE_FROM = """
    FROM graph_edges e
    LEFT JOIN _cluster_map cm_s ON e.source_node_id = cm_s.id
    LEFT JOIN _cluster_map cm_t ON e.target_node_id = cm_t.id
    WHERE COALESCE(cm_s.cluster_id, e.source_node_id) IN (SELECT id FROM _kept_ids)
      AND COALESCE(cm_t.cluster_id, e.target_node_id) IN (SELECT id FROM _kept_ids)
      AND COALESCE(cm_s.cluster_id, e.source_node_id) != COALESCE(cm_t.cluster_id, e.target_node_id)
"""


_KNOWLEDGE_EDGE_SELECT = (
    "SELECT COALESCE(cm_s.cluster_id, e.source_node_id) AS source, "
    "COALESCE(cm_t.cluster_id, e.target_node_id) AS target, e.type, e.weight "
    + _KNOWLEDGE_EDGE_FROM
    + " ORDER BY e.weight DESC LIMIT ?"
)


_KNOWLEDGE_EDGE_COUNT = (
    "SELECT COUNT(*) FROM ("
    "SELECT COALESCE(cm_s.cluster_id, e.source_node_id) AS source, "
    "COALESCE(cm_t.cluster_id, e.target_node_id) AS target, e.type "
    + _KNOWLEDGE_EDGE_FROM
    + " GROUP BY 1, 2, 3)"
)


_KNOWLEDGE_DEGREE_SQL = """
    SELECT node_id, SUM(cnt) AS degree FROM (
        SELECT e.source_node_id AS node_id, COUNT(*) AS cnt
        FROM graph_edges e
        INNER JOIN _visible_ids s ON e.source_node_id = s.id
        INNER JOIN _visible_ids t ON e.target_node_id = t.id
        GROUP BY e.source_node_id
        UNION ALL
        SELECT e.target_node_id AS node_id, COUNT(*) AS cnt
        FROM graph_edges e
        INNER JOIN _visible_ids s ON e.source_node_id = s.id
        INNER JOIN _visible_ids t ON e.target_node_id = t.id
        GROUP BY e.target_node_id
    ) GROUP BY node_id
"""


@router.get("/api/memory/knowledge-graph")
async def get_knowledge_graph(request: Request):
    """Return the knowledge graph from brain.db (graph_nodes + graph_edges).

    Query params:
        min_mentions: Minimum mention_count to include a node (default: 1, show all).
                      Edges where either endpoint is filtered out are also removed.
        cluster:      "auto" (default) clusters dust nodes when total > 2000,
                      "off" disables clustering, "on" forces it.
        max_nodes:    Cap on returned nodes after clustering (default 1500,
                      clamped to 50..20000). Non-integer values are a 400.
        max_edges:    Cap on returned edges after node selection (default 5000,
                      clamped to 100..300000). Edges are ordered by weight DESC
                      so the strongest edges survive. Non-integer values are a 400.

    Stats: total_nodes / total_edges are the SHOWN counts (same as nodes_shown
    / edges_shown). total_nodes_available / total_edges_available describe the
    graph after min_mentions + clustering but before the max_nodes / max_edges
    caps, and truncated is True when the response omits part of that graph.
    """
    try:
        min_mentions = int(request.query_params.get("min_mentions", 1))
    except (TypeError, ValueError):
        return JSONResponse({"error": "min_mentions must be an integer"}, status_code=400)

    max_nodes, err = _parse_bounded_int_param(request, "max_nodes", 1500, 50, 20000)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    max_edges, err = _parse_bounded_int_param(request, "max_edges", 5000, 100, 300000)
    if err:
        return JSONResponse({"error": err}, status_code=400)

    cluster_mode = request.query_params.get("cluster", "auto")
    store = _memory_store()
    if store is None:
        return JSONResponse({"error": _memory_off_message()}, status_code=404)
    brain_db_path = store.brain_db

    if not brain_db_path.exists():
        return JSONResponse({"error": "brain.db not found"}, status_code=404)

    try:
        with closing(sqlite3.connect(brain_db_path)) as conn:
            conn.row_factory = sqlite3.Row

            nodes = [dict(r) for r in conn.execute(
                "SELECT id, type, name, description, mention_count as size FROM graph_nodes WHERE mention_count >= ?",
                (min_mentions,)
            ).fetchall()]

            # Degree per visible node, computed in SQL so we never materialize
            # the full edge set just to cluster dust nodes.
            conn.execute("CREATE TEMP TABLE _visible_ids (id INTEGER PRIMARY KEY)")
            conn.executemany("INSERT INTO _visible_ids VALUES (?)", [(n["id"],) for n in nodes])
            degree_rows = conn.execute(_KNOWLEDGE_DEGREE_SQL).fetchall()
            degree = {row["node_id"]: row["degree"] for row in degree_rows}

            # Server-side clustering: aggregate low-value nodes into type-based clusters
            should_cluster = (
                cluster_mode == "on"
                or (cluster_mode == "auto" and len(nodes) > 2000)
            )
            if should_cluster:
                nodes, cluster_id_map = _cluster_dust_nodes(nodes, degree)
            else:
                cluster_id_map = {}

            total_nodes_available = len(nodes)
            # Keep the top max_nodes nodes by size (mention_count; cluster
            # nodes count by their size).
            nodes.sort(key=lambda n: n["size"], reverse=True)
            shown_nodes = nodes[:max_nodes]
            shown_ids = {n["id"] for n in shown_nodes}

            conn.execute("CREATE TEMP TABLE _cluster_map (id INTEGER PRIMARY KEY, cluster_id INTEGER)")
            conn.executemany("INSERT INTO _cluster_map VALUES (?, ?)", list(cluster_id_map.items()))
            conn.execute("CREATE TEMP TABLE _kept_ids (id INTEGER PRIMARY KEY)")
            conn.executemany("INSERT INTO _kept_ids VALUES (?)", [(nid,) for nid in shown_ids])

            edges = [dict(r) for r in conn.execute(_KNOWLEDGE_EDGE_SELECT, (max_edges,)).fetchall()]

            # Remapping dust endpoints onto a cluster can collapse distinct DB
            # edges into the same (source, target, type) triple; keep the first,
            # which is the strongest because the query orders by weight DESC.
            seen_edge_keys = set()
            deduped_edges = []
            for e in edges:
                key = (e["source"], e["target"], e["type"])
                if key in seen_edge_keys:
                    continue
                seen_edge_keys.add(key)
                deduped_edges.append(e)
            edges = deduped_edges

            # Count the available edges (same remap/dedup rules, before caps).
            conn.execute("DELETE FROM _kept_ids")
            conn.executemany("INSERT INTO _kept_ids VALUES (?)", [(n["id"],) for n in nodes])
            total_edges_available = conn.execute(_KNOWLEDGE_EDGE_COUNT).fetchone()[0]

        truncated = (len(shown_nodes) < total_nodes_available) or (len(edges) < total_edges_available)

        return {
            "nodes": shown_nodes,
            "edges": edges,
            "stats": {
                "total_nodes": len(shown_nodes),
                "total_edges": len(edges),
                "clustered": should_cluster,
                "total_nodes_available": total_nodes_available,
                "total_edges_available": total_edges_available,
                "nodes_shown": len(shown_nodes),
                "edges_shown": len(edges),
                "truncated": truncated,
            }
        }
    except Exception as e:
        logger.error(f"knowledge graph error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/memory/build-status")
async def get_memory_build_status():
    """Read-only status of the memory store's scheduled graph build.

    Reads the JSON receipt the build writes, at [memory] build_receipt under
    the store's root, and shows [memory] build_schedule. "unknown" when the
    integration or the receipt isn't configured, or the receipt is missing,
    unreadable or invalid. Never presented as fresh.
    """
    store = _memory_store()
    schedule = store.build_schedule if store else None
    receipt_path = store.root / store.build_receipt if store and store.build_receipt else None
    unknown = {
        "state": "unknown",
        "started_at": None,
        "finished_at": None,
        "exit_status": None,
        "invocation": None,
        "schedule": schedule,
    }
    try:
        if receipt_path is None or not receipt_path.is_file():
            return unknown
        with open(receipt_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return unknown
        exit_status = data.get("exit_status")
        if isinstance(exit_status, bool) or not isinstance(exit_status, int):
            return unknown
        return {
            "state": "ok" if exit_status == 0 else "failed",
            "started_at": data.get("started_at") if isinstance(data.get("started_at"), str) else None,
            "finished_at": data.get("finished_at") if isinstance(data.get("finished_at"), str) else None,
            "exit_status": exit_status,
            "invocation": data.get("invocation") if isinstance(data.get("invocation"), str) else None,
            "schedule": schedule,
        }
    except (OSError, ValueError):
        return unknown


def _cluster_dust_nodes(nodes: list[dict], degree: dict) -> tuple[list[dict], dict]:
    """Collapse low-signal nodes into per-type aggregate clusters.

    Dust = mention_count <= 2 AND degree <= 5. These get grouped by type
    into a single cluster node per type, reducing rendered node count.
    Returns (nodes_with_clusters, {dust_node_id: cluster_id}).
    """
    from collections import defaultdict

    dust_by_type = defaultdict(list)
    for n in nodes:
        if n["size"] <= 2 and degree.get(n["id"], 0) <= 5:
            dust_by_type[n["type"]].append(n)

    if not dust_by_type:
        return nodes, {}

    # Build cluster nodes (negative IDs to avoid collisions)
    cluster_id_map = {}  # dust node id -> cluster id
    cluster_nodes = []
    for i, (ntype, dust_nodes) in enumerate(dust_by_type.items()):
        if len(dust_nodes) < 3:
            continue  # not worth clustering 1-2 nodes
        cluster_id = -(i + 1)
        for dn in dust_nodes:
            cluster_id_map[dn["id"]] = cluster_id
        cluster_nodes.append({
            "id": cluster_id,
            "type": ntype,
            "name": f"{len(dust_nodes)} {ntype}s (low-ref)",
            "description": f"Cluster of {len(dust_nodes)} {ntype} nodes with low references",
            "size": len(dust_nodes),
            "is_cluster": True,
            "cluster_count": len(dust_nodes),
        })

    # Keep non-dust nodes + add cluster nodes
    kept_nodes = [n for n in nodes if n["id"] not in cluster_id_map]
    kept_nodes.extend(cluster_nodes)
    return kept_nodes, cluster_id_map


@router.get("/api/memory/types")
async def get_memory_types():
    """Return distinct thought types in the configured memory store's `thoughts` table.

    Used by memory.js and memory_svg.js for the legend and filter dropdowns.
    """
    import sqlite3

    store = _memory_store()
    brain_db_path = store.brain_db if store else None

    if brain_db_path is None or not brain_db_path.exists():
        return {"types": ["observation", "decision", "idea", "question"]}

    try:
        with closing(sqlite3.connect(brain_db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT DISTINCT json_extract(metadata, '$.type') AS thought_type FROM thoughts "
                "WHERE metadata IS NOT NULL ORDER BY thought_type"
            ).fetchall()

        types = sorted(set(
            row["thought_type"] for row in rows
            if row["thought_type"] is not None
        ))
        # Always ensure the canonical types are present
        for t in ("observation", "decision", "idea", "question"):
            if t not in types:
                types.insert(0, t)

        return {"types": types}

    except Exception as e:
        logger.error(f"Error reading memory types: {e}")
        return {"types": ["observation", "decision", "idea", "question"]}


@router.get("/api/memory/heatmap")
async def get_memory_heatmap():
    """Return thought density grouped by date and type for the heatmap view.

    Reads the configured memory store's `thoughts` table; used by
    memory_heatmap.js. Returns rows of { date, type, count } sorted chronologically.
    """
    import sqlite3

    store = _memory_store()
    if store is None:
        return {"rows": [], "error": _memory_off_message()}
    brain_db_path = store.brain_db

    if not brain_db_path.exists():
        return {"rows": [], "error": "Memory database not found"}

    try:
        with closing(sqlite3.connect(brain_db_path)) as conn:
            conn.row_factory = sqlite3.Row

            rows = conn.execute("""
                SELECT
                    date(created_at) AS day,
                    json_extract(metadata, '$.type') AS thought_type,
                    COUNT(*) AS count
                FROM thoughts
                WHERE metadata IS NOT NULL
                GROUP BY day, thought_type
                ORDER BY day ASC, thought_type ASC
            """).fetchall()

        return {
            "rows": [
                {
                    "date": row["day"],
                    "type": row["thought_type"] or "observation",
                    "count": row["count"]
                }
                for row in rows
            ]
        }

    except Exception as e:
        logger.error(f"Error reading memory heatmap: {e}")
        return JSONResponse({"error": f"Error reading heatmap data: {e}"}, status_code=500)


@router.get("/api/thoughts")
def get_thoughts(request: Request):
    """Return a page of thoughts from the configured store's brain.db, newest first.

    For the Memory > List view (memory_list.js). ``query`` matches content,
    ``thought_type`` the metadata type; ``limit`` (1-200, default 50) and
    ``offset`` page through ``total`` matches. Off or missing: the heatmap's shape.
    """
    limit, err = _parse_bounded_int_param(request, "limit", 50, 1, 200)
    if err is None:
        offset, err = _parse_bounded_int_param(request, "offset", 0, 0, 2**62)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    store = _memory_store()
    if store is None:
        return {"thoughts": [], "total": 0, "error": _memory_off_message()}
    if not store.brain_db.exists():
        return {"thoughts": [], "total": 0, "error": "Memory database not found"}

    query = request.query_params.get("query", "")
    thought_type = request.query_params.get("thought_type", "")
    if "\0" in query + thought_type:
        # SQLite ends a string at NUL, which would silently drop the filter.
        return JSONResponse({"error": "query and thought_type must not contain NUL"}, status_code=400)
    where, args = ["1 = 1"], []
    if query:
        where.append("content LIKE ? ESCAPE '\\'")
        args.append("%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
    if thought_type:
        where.append("json_extract(metadata, '$.type') = ?")
        args.append(thought_type)
    sql_where = " WHERE " + " AND ".join(where)
    try:
        with closing(sqlite3.connect(f"{store.brain_db.as_uri()}?mode=ro", uri=True)) as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM thoughts{sql_where}", args).fetchone()[0]
            rows = conn.execute(
                "SELECT id, content, json_extract(metadata, '$.type'), json_extract(metadata, '$.project'),"
                f" created_at FROM thoughts{sql_where} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                [*args, limit, offset]).fetchall()
    except sqlite3.Error as e:
        logger.error(f"Error reading thoughts: {e}")
        return JSONResponse({"error": f"Error reading thoughts: {e}"}, status_code=500)
    return {
        "thoughts": [{"id": r[0], "content": r[1], "type": "observation" if r[2] is None else str(r[2]), "project": r[3],
                      "created_at": r[4]} for r in rows],
        "total": total, "limit": limit, "offset": offset,
    }


@router.get("/api/graph")
async def get_graph_data(
    project: Optional[str] = None,      # Filter to single project
    file_types: Optional[str] = None,   # Comma-separated: "py,ts,md"
    include_orphans: bool = True,       # Show orphaned nodes
    min_connections: int = 0            # Filter by connection count
):
    """Return graph JSON for D3.js visualization."""
    import json
    graph_path = Path(__file__).parents[2] / "data" / "graph.json"  # the project root, as from dashboard/app.py
    if not graph_path.exists():
        return JSONResponse({
            "error": "Graph not built. Run: python scripts/discovery/graph_builder.py"
        }, status_code=404)

    try:
        graph = json.loads(graph_path.read_text())
    except Exception as e:
        logger.error(f"Error reading graph.json: {e}")
        return JSONResponse({"error": f"Error reading graph data: {e}"}, status_code=500)

    # Apply filters
    nodes = graph.get("nodes", [])
    edges = graph.get("edges", [])

    # Filter by project
    if project:
        nodes = [n for n in nodes if n.get("project") == project]
        node_ids = {n["id"] for n in nodes}
        edges = [e for e in edges if e["source"] in node_ids and e["target"] in node_ids]

    # Filter by file types
    if file_types:
        allowed_types = [t.strip().lower() for t in file_types.split(',')]
        nodes = [n for n in nodes if n.get("type") in allowed_types]
        node_ids = {n["id"] for n in nodes}
        edges = [e for e in edges if e["source"] in node_ids and e["target"] in node_ids]

    # Filter orphans
    if not include_orphans:
        nodes = [n for n in nodes if not n.get("is_orphan")]
        node_ids = {n["id"] for n in nodes}
        edges = [e for e in edges if e["source"] in node_ids and e["target"] in node_ids]

    # Filter by min connections
    if min_connections > 0:
        nodes = [n for n in nodes if n.get("size", 0) >= min_connections]
        node_ids = {n["id"] for n in nodes}
        edges = [e for e in edges if e["source"] in node_ids and e["target"] in node_ids]

    return {
        "generated_at": graph.get("generated_at"),
        "stats": {
            "total_nodes": len(nodes),
            "total_edges": len(edges),
            "orphan_count": len([n for n in nodes if n.get("is_orphan")])
        },
        "nodes": nodes,
        "edges": edges
    }
