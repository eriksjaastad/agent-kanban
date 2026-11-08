"""The dashboard's tracked graph rebuild reports a failed rebuild as an error (#8677).

rebuild_project_graph() raises, and _run_tracked_rebuild records the failure
for /api/rebuild-status instead of reporting "done" over the old graph.
"""

import pytest

import dashboard.app as dashboard_app
import dashboard.routes.memory as memory_routes  # noqa: E402
import pt


@pytest.mark.parametrize("graph_section, status", [
    ('[graph]\nskip_dirs = ["ok", 3]\n', "error"),
    ('[graph]\nskip_dirs = ["scratch"]\n', "done"),
    ("", "done"),
])
def test_the_tracked_rebuild_reports_what_the_rebuild_did(tmp_path, monkeypatch, graph_section, status):
    (tmp_path / "projects" / "app").mkdir(parents=True)
    (tmp_path / "projects" / "app" / "main.py").write_text("")
    (tmp_path / "config.toml").write_text(graph_section)
    monkeypatch.setenv("PT_CONFIG", str(tmp_path / "config.toml"))
    graph = tmp_path / "graph.json"
    graph.write_text("old")
    monkeypatch.setattr(pt, "GRAPH_PATH", graph)
    monkeypatch.setattr(pt, "GRAPH_ANALYSIS_PATH", tmp_path / "analysis.md")
    monkeypatch.setattr(pt, "PROJECTS_BASE_DIR", str(tmp_path / "projects"))

    job_id = memory_routes._create_rebuild_job("project_graph")
    memory_routes._run_tracked_rebuild(job_id, pt.rebuild_project_graph)
    job = memory_routes._rebuild_jobs[job_id]
    assert job["status"] == status
    if status == "error":
        assert "[graph]" in job["error"] and graph.read_text() == "old"
    else:
        assert job["error"] is None and graph.read_text() != "old"
