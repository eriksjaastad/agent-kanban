"""Tests for /api/agentic/markers and the shared markers loader.

The markers file is a gitignored JSON list. A missing file is a valid empty
state, but a present-and-unreadable file must never be treated as empty:
the next POST would otherwise overwrite every marker in it (#7586).
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import dashboard.app as dashboard_app
import dashboard.routes.agentic as agentic_routes  # noqa: E402
from fastapi.testclient import TestClient


@pytest.fixture
def markers_file(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "agentic_markers.json"
    monkeypatch.setattr(agentic_routes, "MARKERS_PATH", path)
    return path


def test_get_markers_missing_file_returns_empty_list(markers_file: Path):
    response = TestClient(dashboard_app.app).get("/api/agentic/markers")

    assert response.status_code == 200, response.text
    assert response.json() == {"markers": []}


@pytest.mark.parametrize(
    ("contents", "expected_detail"),
    [
        pytest.param(b"{not-json", "invalid JSON", id="corrupt_json"),
        pytest.param(
            json.dumps({"date": "2026-03-10", "label": "not a list"}).encode(),
            "not a list",
            id="non_list_json",
        ),
        pytest.param(b"\xff\xfe[", "not valid UTF-8", id="invalid_utf8"),
    ],
)
def test_get_markers_unreadable_file_returns_503_with_detail(
    markers_file: Path, contents: bytes, expected_detail: str
):
    markers_file.write_bytes(contents)

    response = TestClient(dashboard_app.app).get("/api/agentic/markers")

    assert response.status_code == 503, response.text
    detail = response.json()["detail"]
    assert str(markers_file) in detail
    assert expected_detail in detail


@pytest.mark.parametrize(
    ("method", "url", "body", "contents"),
    [
        pytest.param(
            "post", "/api/agentic/markers",
            {"date": "2026-03-10", "label": "Would erase everything"},
            b"\xff\xfe[", id="post_with_invalid_utf8",
        ),
        pytest.param(
            "post", "/api/agentic/markers",
            {"date": "2026-03-10", "label": "Would erase everything"},
            b'{"markers": [', id="post_with_corrupt_file",
        ),
        pytest.param(
            "patch", "/api/agentic/markers/some-id",
            {"label": "Would erase everything"},
            b'{"markers": [', id="patch_with_corrupt_file",
        ),
        pytest.param(
            "delete", "/api/agentic/markers/some-id", None,
            b'{"markers": [', id="delete_with_corrupt_file",
        ),
    ],
)
def test_write_with_unreadable_file_returns_503_and_leaves_file_unchanged(
    markers_file: Path, method: str, url: str, body, contents: bytes
):
    markers_file.write_bytes(contents)

    kwargs = {} if body is None else {"json": body}
    response = getattr(TestClient(dashboard_app.app), method)(url, **kwargs)

    assert response.status_code == 503, response.text
    assert markers_file.read_bytes() == contents


def test_marker_crud_round_trip(markers_file: Path):
    client = TestClient(dashboard_app.app)

    # Missing file -> empty list, no error.
    assert client.get("/api/agentic/markers").json() == {"markers": []}

    # Create.
    response = client.post(
        "/api/agentic/markers",
        json={"date": "2026-03-10", "label": "Workflow change"},
    )
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["date"] == "2026-03-10"
    assert created["label"] == "Workflow change"
    assert created["source"] == "manual"
    marker_id = created["id"]

    # Listed and persisted.
    listed = client.get("/api/agentic/markers").json()["markers"]
    assert [m["id"] for m in listed] == [marker_id]

    # Update.
    response = client.patch(
        f"/api/agentic/markers/{marker_id}",
        json={"label": "Workflow change (updated)"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["label"] == "Workflow change (updated)"

    # Delete.
    response = client.delete(f"/api/agentic/markers/{marker_id}")
    assert response.status_code == 204, response.text
    assert client.get("/api/agentic/markers").json() == {"markers": []}
