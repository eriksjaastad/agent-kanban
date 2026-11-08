"""A card's short display ID and its full snowflake ID resolve to the same task.

Moved from the tests of migration 009, which created task_display_ids.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def tasks(db):
    db.add_project("test", "Test Project", "/tmp/test", "active")
    return [db.add_task(f"task {n}", project_id="test") for n in (1, 2)]


def test_every_new_task_gets_a_display_id(db, tasks):
    display_ids = [db.get_task_display_id(task["id"]) for task in tasks]
    assert all(isinstance(display_id, int) for display_id in display_ids)
    assert len(set(display_ids)) == len(display_ids)


def test_either_id_resolves_to_the_task(db, tasks):
    task_id = tasks[0]["id"]
    assert db.resolve_task_id(db.get_task_display_id(task_id)) == task_id
    assert db.resolve_task_id(task_id) == task_id


def test_unknown_ids_resolve_to_none(db, tasks):
    assert db.get_task_display_id(999999999999999) is None
    assert db.resolve_task_id(888888888) is None


def test_display_id_map_covers_exactly_the_ids_asked_for(db, tasks):
    ids = [task["id"] for task in tasks]
    mapping = db.get_task_display_id_map(ids)
    assert mapping == {task_id: db.get_task_display_id(task_id) for task_id in ids}
    assert db.get_task_display_id_map([]) == {}
