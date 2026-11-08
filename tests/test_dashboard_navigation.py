from fastapi.testclient import TestClient

from dashboard.app import app
from dashboard.common import build_navigation, build_spa_shell_html, iter_navigation_leaves


client = TestClient(app)


def leaf_by_id(items, leaf_id):
    """Find a leaf navigation item by id, descending through groups."""
    return next(item for item in iter_navigation_leaves(items) if item["id"] == leaf_id)


def group_by_id(items, group_id):
    return next(item for item in items if item["id"] == group_id)


def active_ids(items):
    """Collect the ids of every active top-level item, group, and child."""
    ids = []
    for item in items:
        if item.get("active"):
            ids.append(item["id"])
        for child in item.get("children", []):
            if child.get("active"):
                ids.append(child["id"])
    return set(ids)


def test_root_redirects_to_dashboard():
    response = client.get("/", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "/dashboard"


def test_navigation_api_returns_shared_contract():
    response = client.get("/api/navigation")

    assert response.status_code == 200

    payload = response.json()
    assert payload["title"] == "Project Tracker"
    items = payload["items"]
    assert [item["id"] for item in items] == [
        "morning",
        "dashboard",
        "group-kanban",
        "group-jobs",
        "group-agents",
        "group-memory",
        "reminders",
    ]
    assert items[0]["href"] == "/morning"
    assert items[0]["navigation_type"] == "spa"

    children_by_group = {
        item["id"]: [child["id"] for child in item.get("children", [])]
        for item in items
    }
    assert children_by_group["group-kanban"] == ["kanban", "calendar"]
    assert children_by_group["group-jobs"] == ["jobs", "jobs-submitted", "jobs-volunteer"]
    assert children_by_group["group-agents"] == ["agent-chat", "agentic", "codebase"]
    assert children_by_group["group-memory"] == ["memory", "graph"]

    for group in items:
        if not group["id"].startswith("group-"):
            continue
        first_child = group["children"][0]
        assert group["href"] == first_child["href"]
        assert group["navigation_type"] == first_child["navigation_type"]
        assert group["match_prefixes"] == [
            prefix for child in group["children"] for prefix in child["match_prefixes"]
        ]

    leaves = {item["id"]: item for item in iter_navigation_leaves(items)}
    assert leaves["morning"]["label"] == "Morning"
    assert leaves["dashboard"]["label"] == "Dashboard"
    assert leaves["kanban"]["label"] == "Board"
    assert leaves["calendar"]["label"] == "Calendar"
    assert leaves["jobs"]["label"] == "Listings"
    assert leaves["jobs-submitted"]["label"] == "Submissions"
    assert leaves["jobs-volunteer"]["label"] == "Volunteer"
    assert leaves["agent-chat"]["label"] == "Chat"
    assert leaves["agentic"]["label"] == "Autonomy"
    assert leaves["codebase"]["label"] == "Codebase size"
    assert leaves["memory"]["label"] == "Memory"
    assert leaves["graph"]["label"] == "Graph"


def test_build_navigation_marks_expected_active_items():
    project_navigation = build_navigation("/project/project-tracker")
    kanban_navigation = build_navigation("/kanban/project-tracker")

    assert next(item for item in project_navigation if item["id"] == "dashboard")["active"] is True
    assert group_by_id(project_navigation, "group-kanban")["active"] is False
    assert leaf_by_id(kanban_navigation, "kanban")["active"] is True
    assert leaf_by_id(kanban_navigation, "calendar")["active"] is False
    assert group_by_id(kanban_navigation, "group-kanban")["active"] is True


def test_child_paths_activate_only_the_matching_group_and_child():
    cases = {
        "/jobs/submitted": {"group-jobs", "jobs-submitted"},
        "/jobs/volunteer": {"group-jobs", "jobs-volunteer"},
        "/kanban/project-tracker": {"group-kanban", "kanban"},
        "/graph": {"group-memory", "graph"},
    }
    for path, expected_ids in cases.items():
        assert active_ids(build_navigation(path)) == expected_ids, path


def test_project_path_still_activates_dashboard():
    assert active_ids(build_navigation("/project/project-tracker")) == {"dashboard"}


def test_graph_view_renders_shared_shell_navigation():
    response = client.get("/graph")

    assert response.status_code == 200
    body = response.text
    assert "Project Tracker" in body
    assert "Morning" in body
    assert "Dashboard" in body
    assert "Kanban" in body
    assert "Jobs" in body
    assert "Agents" in body
    assert "Memory" in body
    assert 'href="/jobs/submitted"' in body
    assert "Board" in body
    assert "Calendar" in body
    assert "Listings" in body
    assert "Submissions" in body
    assert "Chat" in body
    assert "Autonomy" in body
    assert "Code reviews" not in body
    assert "Graph" in body
    assert "Project Graph" in body

    # The Jinja nav mirrors the React nav: a real caret button per group,
    # menu roles, and the JS that makes menus touch-openable.
    assert (
        '<button type="button" class="nav-caret" aria-haspopup="true" '
        'aria-expanded="false" aria-label="Jobs menu">' in body
    )
    assert 'role="menu"' in body
    assert '<script src="/static/nav.js" defer></script>' in body

    nav_js = client.get("/static/nav.js")
    assert nav_js.status_code == 200


def test_build_spa_shell_html_bootstraps_backend_navigation_payload():
    html = "<html><head></head><body><div id='root'></div></body></html>"

    rendered = build_spa_shell_html(html, "/agentic")

    assert "window.__PT_NAVIGATION__" in rendered
    assert '"title": "Project Tracker"' in rendered
    assert '"href": "/agentic"' in rendered
    assert '"active": true' in rendered


def test_agent_chat_spa_route_is_registered():
    """SPA shell is 200 when dist exists, 503 with the usual hint when it does not (CI)."""
    response = client.get("/agent-chat")
    assert response.status_code in (200, 503)
    if response.status_code == 200:
        assert "window.__PT_NAVIGATION__" in response.text
        assert '"id": "agent-chat"' in response.text
    else:
        assert "Frontend not built" in response.text


def test_dead_dashboard_endpoints_are_no_longer_served():
    """#8093: legacy action endpoints, the unused cache, /project and /old are gone."""
    gets = (
        "/old", "/project/project-tracker", "/api/telemetry", "/api/backup",
        "/api/learning", "/api/loops", "/api/agents", "/api/stats",
        "/api/dashboard-cache-status", "/api/api-cost-stats",
    )
    posts = ("/api/create-index/x", "/api/fix-frontmatter/x", "/api/agents/run")
    for path in gets:
        assert client.get(path).status_code == 404, path
    for path in posts:
        assert client.post(path, json={}).status_code in (404, 405), path
    # The removed DELETE /api/tasks/done must not fall through to the task-id
    # routes (which would answer 422): task ids only match integers.
    for method in ("get", "patch", "delete"):
        response = client.request(method.upper(), "/api/tasks/done")
        assert response.status_code == 404, method
        assert response.json()["detail"] == "Not Found", method
    # Integer ids still reach the task handler rather than the router's 404.
    integer = client.get("/api/tasks/999999999")
    assert not (integer.status_code == 404 and integer.json().get("detail") == "Not Found")
    # /api/refresh is the one legacy action that stays; the Memory and Graph pages too.
    assert client.get("/memory").status_code == 200
    assert client.get("/graph").status_code == 200


def test_codebase_spa_route_and_navigation():
    response = client.get("/codebase")
    assert response.status_code in (200, 503)
    nav = client.get("/api/navigation").json()["items"]
    item = leaf_by_id(nav, "codebase")
    assert item["href"] == "/codebase"
    assert item["navigation_type"] == "spa"
    assert leaf_by_id(build_navigation("/codebase"), "codebase")["active"]
    if response.status_code == 200:
        assert '"id": "codebase"' in response.text


def test_reminders_is_a_top_level_tab_on_spa_and_document_pages():
    response = client.get("/reminders")
    assert response.status_code in (200, 503)
    item = next(i for i in client.get("/api/navigation").json()["items"] if i["id"] == "reminders")
    assert (item["label"], item["href"], item["navigation_type"]) == (
        "Reminders", "/reminders", "spa")
    assert leaf_by_id(build_navigation("/reminders"), "reminders")["active"]
    # The Jinja shell (/memory, /graph) renders the same top-level tab.
    assert 'href="/reminders"' in client.get("/graph").text
