from fastapi.testclient import TestClient

from dashboard.app import app
from dashboard.common import build_navigation, build_spa_shell_html, iter_navigation_leaves
from scripts.addon_registry import enabled as enabled_addons


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
    # The core's entries, then each enabled add-on's (none when addons/ is empty):
    # an add-on entry that names a group goes last in that group.
    addon_nav = [entry for addon in enabled_addons() for entry in addon.nav]
    addon_ids = [entry["id"] for entry in addon_nav if "group" not in entry]

    def added_to(group_id):
        return [entry["id"] for entry in addon_nav if entry.get("group") == group_id]
    assert [item["id"] for item in items] == [
        "morning",
        "dashboard",
        "group-kanban",
        "group-jobs",
        "group-agents",
        "group-memory",
        *addon_ids,
    ]
    assert items[0]["href"] == "/morning"
    assert items[0]["navigation_type"] == "spa"

    children_by_group = {
        item["id"]: [child["id"] for child in item.get("children", [])]
        for item in items
    }
    assert children_by_group["group-kanban"] == ["kanban", "calendar", *added_to("group-kanban")]
    assert children_by_group["group-jobs"] == [
        "jobs", "jobs-submitted", "jobs-volunteer", *added_to("group-jobs")]
    assert children_by_group["group-agents"] == ["agent-chat", "agentic", *added_to("group-agents")]
    assert children_by_group["group-memory"] == ["memory", "graph", *added_to("group-memory")]

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


def test_no_dropdown_ever_has_one_item():
    from dashboard.common import collapse_navigation

    def leaf(leaf_id, **extra):
        return {"id": leaf_id, "label": leaf_id.title(), "href": f"/{leaf_id}",
                "match_prefixes": [f"/{leaf_id}"], "navigation_type": "spa", **extra}

    items = [
        leaf("home"),
        {"id": "group-empty", "label": "Empty", "children": []},
        {"id": "group-one", "label": "One", "children": [leaf("board", solo_label="Kanban Board")]},
        {"id": "group-plain", "label": "Plain", "children": [leaf("calendar")]},
        {"id": "group-two", "label": "Two", "children": [leaf("a", solo_label="Solo A"), leaf("b")]},
    ]
    collapsed = collapse_navigation(items)
    assert [(item["id"], item["label"]) for item in collapsed] == [
        ("home", "Home"), ("board", "Kanban Board"), ("calendar", "Calendar"), ("group-two", "Two")]
    assert "children" not in collapsed[1] and "solo_label" not in collapsed[1]
    assert [child["label"] for child in collapsed[3]["children"]] == ["A", "B"]


def test_a_promoted_item_is_a_plain_tab_in_both_menus(monkeypatch):
    import dashboard.common as common

    # Leave Agents with one item, as if the others were add-ons turned off.
    items = [dict(item) for item in common.NAVIGATION_ITEMS]
    agents = next(item for item in items if item["id"] == "group-agents")
    agents["children"] = [child for child in agents["children"] if child["id"] == "agent-chat"]
    monkeypatch.setattr(common, "NAVIGATION_ITEMS", common.collapse_navigation(items))

    nav = client.get("/api/navigation").json()["items"]
    chat = next(item for item in nav if item["id"] == "agent-chat")
    assert (chat["label"], chat["href"], "children" in chat) == ("Agent Chat", "/agent-chat", False)
    assert not any(item["id"] == "group-agents" for item in nav)
    assert leaf_by_id(common.build_navigation("/agent-chat"), "agent-chat")["active"]
    page = client.get("/graph").text
    assert 'href="/agent-chat"' in page and "Agent Chat" in page
