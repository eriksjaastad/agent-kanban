"""The dashboard's memory views follow the private local config (#8677).

Why these exist: every memory endpoint must answer from the configured store,
say "not configured" in its own shape when the integration is off, and
report a broken config, and the static pages must show what the server said
rather than an invented schedule or a cached empty view.
"""

import json
import sqlite3
import subprocess
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

import dashboard.app as dashboard_app
import dashboard.routes.memory as memory_routes  # noqa: E402

STATIC = Path(__file__).resolve().parent.parent / "dashboard" / "static"


def make_brain(root: Path, contents: str) -> Path:
    """A brain.db at root with one dated thought of type "configured-only"."""
    root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(root / "brain.db")
    conn.execute("CREATE TABLE thoughts (id INTEGER PRIMARY KEY, content TEXT, embedding TEXT, "
                 "embedding_blob BLOB, metadata TEXT, created_at TEXT, source_machine TEXT)")
    vector = np.array([1.0, 0.0], dtype="<f4").tobytes()
    # A type that is not in the endpoint's default list, so reading it proves the store was read.
    conn.execute("INSERT INTO thoughts (id, content, embedding_blob, metadata, created_at, source_machine) "
                 "VALUES (1, ?, ?, '{\"type\": \"configured-only\"}', '2026-10-27T10:00:00', 'test')", (contents, vector))
    conn.commit()
    conn.close()
    return root / "brain.db"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setitem(memory_routes._graph_cache, "data", None)
    return TestClient(dashboard_app.app)


# (path, status when off, check of the off body)
ENDPOINTS = [
    ("/api/memory/knowledge-graph", 404, lambda b: "not configured" in b["error"]),
    ("/api/memory-graph", 404, lambda b: "not configured" in b["error"]),
    ("/api/memory/types", 200, lambda b: b["types"] == ["observation", "decision", "idea", "question"]),
    ("/api/memory/heatmap", 200, lambda b: b["rows"] == [] and "not configured" in b["error"]),
    ("/api/thoughts", 200, lambda b: b["thoughts"] == [] and b["total"] == 0 and "not configured" in b["error"]),
    ("/api/memory/build-status", 200, lambda b: b["state"] == "unknown" and b["schedule"] is None),
    ("/api/tool-stats?tool=WebSearch", 503, lambda b: "not configured" in b["detail"]),
    ("/api/bash-stats", 503, lambda b: "not configured" in b["detail"]),
]


@pytest.mark.parametrize("path, status, check", ENDPOINTS, ids=[e[0] for e in ENDPOINTS])
def test_off_is_reported_in_each_endpoints_own_shape(client, path, status, check):
    response = client.get(path)
    assert response.status_code == status, response.text
    assert check(response.json())


@pytest.mark.parametrize("path", [e[0] for e in ENDPOINTS])
def test_a_broken_config_is_a_500_naming_the_file(client, tmp_path, monkeypatch, path):
    config_file = tmp_path / "_local" / "config.toml"
    config_file.write_text("[memory\n")
    monkeypatch.setenv("PT_CONFIG", str(config_file))
    response = client.get(path)
    assert response.status_code == 500 and str(config_file) in response.json()["detail"]


def test_types_and_heatmap_read_the_configured_store(client, memory_store):
    make_brain(memory_store.root, "configured")
    assert "configured-only" in client.get("/api/memory/types").json()["types"]
    rows = client.get("/api/memory/heatmap").json()["rows"]
    assert {(r["date"], r["type"], r["count"]) for r in rows} == {("2026-10-27", "configured-only", 1)}


def test_the_graph_cache_follows_the_configured_store(client, tmp_path, monkeypatch, memory_store):
    make_brain(memory_store.root, "store a")
    first = client.get("/api/memory-graph").json()
    assert client.get("/api/memory-graph").json() == first  # same store: served from cache

    other = tmp_path / "other"
    make_brain(other, "store b")
    (tmp_path / "_local" / "config.toml").write_text(f'[memory]\nroot = "{other}"\n')
    assert client.get("/api/memory-graph").json()["nodes"][0]["content"] == "store b"

    monkeypatch.setenv("PT_CONFIG", str(tmp_path / "no-config.toml"))  # integration off
    assert client.get("/api/memory-graph").status_code == 404

    monkeypatch.setenv("PT_CONFIG", str(tmp_path / "_local" / "config.toml"))
    (tmp_path / "_local" / "config.toml").write_text("[memory\n")
    assert client.get("/api/memory-graph").status_code == 500


def run_js(script: str, body: str):
    """Load a static script in node with stub browser globals, run body, return its JSON result."""
    harness = f"""
const vm = require('vm');
const fs = require('fs');
const elements = {{}};
const element = () => ({{ innerHTML: '', textContent: '', style: {{}},
    replaceChildren(...kids) {{ this.children = kids; this.innerHTML = ''; }},
    appendChild(kid) {{ (this.children = this.children || []).push(kid); }} }});
const context = {{
  console, JSON, Date, isNaN, Math, URLSearchParams,
  document: {{
    addEventListener() {{}},
    getElementById: (id) => elements[id] || (elements[id] = element()),
    createElement: () => element(),
  }},
}};
vm.createContext(context);
vm.runInContext(fs.readFileSync({json.dumps(str(STATIC / script))}, 'utf8'), context);
(async () => {{
  const result = await vm.runInContext({json.dumps(body)}, context);
  process.stdout.write(JSON.stringify(result));
}})().catch((e) => {{ console.error(e); process.exit(1); }});
"""
    out = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.mark.parametrize("schedule, expected", [
    (None, "Graph last built"),
    ("daily 06:00", " · scheduled daily 06:00"),
    ("Mondays 10:00", " · scheduled Mondays 10:00"),
])
def test_build_status_text_shows_only_the_configured_schedule(schedule, expected):
    data = {"state": "ok", "started_at": "2026-10-27T10:00:00Z", "finished_at": "2026-10-27T10:30:00Z",
            "exit_status": 0, "schedule": schedule}
    text = str(run_js("memory_overlay.js", f"formatBuildStatus({json.dumps(data)})"))
    assert expected in text and "weekly" not in text and "Mon 10:00" not in text.replace("Mondays 10:00", "")
    if schedule is None:
        assert "scheduled" not in text


def test_heatmap_shows_an_unreadable_200_body_and_tries_again():
    body = """
(async () => {
  const replies = [
    { ok: true, status: 200, json: async () => { throw new SyntaxError('Unexpected end of JSON input'); } },
    { ok: true, status: 200, json: async () => ({ rows: [] }) },
  ];
  fetch = async () => replies.shift();
  await loadHeatmap();
  const container = document.getElementById('heatmap-container');
  const afterError = { html: container.innerHTML, loaded: heatmapLoaded };
  await loadHeatmap();
  return { afterError, afterRetry: { html: container.innerHTML, loaded: heatmapLoaded } };
})()
"""
    result = run_js("memory_heatmap.js", body)
    assert result["afterError"]["loaded"] is False and "Failed to load heatmap data" in result["afterError"]["html"]
    assert result["afterRetry"]["loaded"] is True and "No heatmap data available" in result["afterRetry"]["html"]


def test_heatmap_shows_a_failed_load_and_tries_again():
    body = """
(async () => {
  const replies = [
    { ok: false, status: 500, json: async () => ({ detail: '/cfg/config.toml is not valid TOML' }) },
    { ok: true, status: 200, json: async () => ({ rows: [] }) },
  ];
  fetch = async () => replies.shift();
  await loadHeatmap();
  const container = document.getElementById('heatmap-container');
  const afterError = { text: container.children[0].textContent, loaded: heatmapLoaded };
  await loadHeatmap();
  return { afterError, afterRetry: { html: container.innerHTML, loaded: heatmapLoaded } };
})()
"""
    result = run_js("memory_heatmap.js", body)
    assert result["afterError"] == {"text": "Error: /cfg/config.toml is not valid TOML", "loaded": False}
    assert result["afterRetry"]["loaded"] is True and "No heatmap data available" in result["afterRetry"]["html"]


def test_memory_page_alerts_the_servers_detail_on_a_500():
    """memory.js shows a 500's `detail` (a broken config) instead of crashing on missing nodes."""
    script = (STATIC / "memory.js").read_text()
    harness = """
const vm = require('vm');
const anything = () => new Proxy(function () {}, {
  get: (t, k) => (k === Symbol.toPrimitive ? () => 0 : anything()),
  apply: () => anything(),
});
const alerts = [];
const context = { console, JSON, Promise, Date, Math, alert: (m) => alerts.push(m),
  performance: { now: () => 0 }, window: anything(), document: anything(), d3: anything(),
  fetch: async (url) => ({ ok: false, status: 500,
    json: async () => ({ detail: '/cfg/config.toml is not valid TOML' }) }) };
vm.createContext(context);
vm.runInContext(SCRIPT, context);
vm.runInContext('loadGraph()', context).then(() => process.stdout.write(JSON.stringify(alerts)));
""".replace("SCRIPT", json.dumps(script))
    out = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == ["Error: /cfg/config.toml is not valid TOML"]
