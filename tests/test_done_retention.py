"""Done-column retention: archive, never delete (#6870).

`pt tasks done` used to call `trim_done_tasks(keep=75)`, which counted Done
cards portfolio-wide with no project filter and hard-DELETEd everything past
75. A busy project's completions evicted a quiet project's history — 1,288
Done cards were destroyed, 244 of them project-tracker's — and because the
delete cascaded to `task_history` it erased its own evidence.

These tests pin the replacement behavior: retention is a *display* concern.
Cards past the per-project cap get `archived_at` stamped and drop off the
board; the rows, their status, and their completion timestamps all survive.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from db.manager import DatabaseManager  # noqa: E402
import pt as pt_cli  # noqa: E402


def _setup_db() -> DatabaseManager:
    """The local manager for this test's own database.

    The isolated database fixture supplies a fresh migrated schema. Nothing
    here opens a file.
    """
    db = DatabaseManager()
    db.add_project("alpha", "Alpha", "/tmp/alpha", "active")
    db.add_project("beta", "Beta", "/tmp/beta", "active")
    return db


def _add_done(db: DatabaseManager, project_id: str, n: int) -> list[int]:
    """Create `n` Done cards in `project_id`, oldest first, with distinct
    completion timestamps so the retention ordering is unambiguous.

    Backdating uses helpers attached only to the synthetic test fixture.
    """
    ids = []
    for i in range(n):
        task = db.add_task(f"{project_id} done {i}", project_id, status="Done")
        stamp = f"2026-01-{(i % 28) + 1:02d}T{(i % 24):02d}:00:00"
        db.seed(task["id"], completed_at=stamp, updated_at=stamp)
        ids.append(task["id"])
    return ids


# ---------------------------------------------------------------------
# (a) completing a card deletes nothing
# ---------------------------------------------------------------------


def test_pt_tasks_done_deletes_zero_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _setup_db()
    monkeypatch.setenv("PT_ALLOW_FRESH_DB", "1")

    # Well past the old keep=75 portfolio cap and the new per-project cap.
    _add_done(db, "alpha", 80)
    _add_done(db, "beta", 5)

    finishing = db.add_task("Finish me", "alpha", status="Review")

    tasks_before = db.count_rows("tasks")
    history_before = db.count_rows("task_history")
    audit_before = db.count_rows("delete_audit_log")

    result = CliRunner().invoke(pt_cli.tasks_group, ["done", str(finishing["id"])])
    assert result.exit_code == 0, result.output

    assert db.count_rows("tasks") == tasks_before
    # task_history only grows — the completion event is appended, never trimmed.
    assert db.count_rows("task_history") >= history_before
    assert db.count_rows("delete_audit_log") == audit_before

    # And it archived rather than trimmed.
    assert "Archived" in result.output
    assert "Trimmed" not in result.output


# ---------------------------------------------------------------------
# (b) archiving is per-project
# ---------------------------------------------------------------------


def test_archiving_is_per_project(tmp_path: Path) -> None:
    db = _setup_db()
    _add_done(db, "alpha", 40)
    beta_ids = _add_done(db, "beta", 3)

    archived = db.archive_done_tasks(keep_per_project=25)
    assert archived == 15  # alpha's oldest 15; beta untouched

    alpha_visible = db.get_tasks(project_id="alpha", status="Done")
    beta_visible = db.get_tasks(project_id="beta", status="Done")
    assert len(alpha_visible) == 25
    assert len(beta_visible) == 3

    # Beta's cards specifically were not archived by alpha's burst.
    assert {t["id"] for t in beta_visible} == set(beta_ids)
    assert all(t["archived_at"] is None for t in beta_visible)

    # The newest alpha cards are the ones kept.
    kept_stamps = sorted(t["completed_at"] for t in alpha_visible)
    all_alpha = db.get_tasks(project_id="alpha", status="Done", include_archived=True)
    archived_stamps = sorted(
        t["completed_at"] for t in all_alpha if t["archived_at"] is not None
    )
    assert max(archived_stamps) <= min(kept_stamps)


def test_archiving_is_idempotent(tmp_path: Path) -> None:
    db = _setup_db()
    _add_done(db, "alpha", 30)

    assert db.archive_done_tasks(keep_per_project=25) == 5
    # Already-archived cards don't count against the cap, so a second run
    # must not archive the next 5.
    assert db.archive_done_tasks(keep_per_project=25) == 0
    assert len(db.get_tasks(project_id="alpha", status="Done")) == 25


def test_archiving_ignores_non_done_cards(tmp_path: Path) -> None:
    db = _setup_db()
    _add_done(db, "alpha", 30)
    backlog = db.add_task("Still open", "alpha", status="Backlog")

    db.archive_done_tasks(keep_per_project=0)

    assert db.get_task(backlog["id"])["archived_at"] is None
    assert len(db.get_tasks(project_id="alpha")) == 1


# ---------------------------------------------------------------------
# (c) get_tasks hides archived by default
# ---------------------------------------------------------------------


def test_get_tasks_hides_archived_by_default(tmp_path: Path) -> None:
    db = _setup_db()
    _add_done(db, "alpha", 30)

    db.archive_done_tasks(keep_per_project=25)

    assert len(db.get_tasks(project_id="alpha")) == 25
    assert len(db.get_tasks(project_id="alpha", include_archived=True)) == 30
    assert len(db.get_tasks()) == 25
    assert len(db.get_tasks(include_archived=True)) == 30

    # The status filter is orthogonal to the archived filter.
    assert len(db.get_tasks(status="Done")) == 25
    assert len(db.get_tasks(status="Done", include_archived=True)) == 30


def test_cli_archived_flag_shows_hidden_cards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _setup_db()
    monkeypatch.setenv("PT_ALLOW_FRESH_DB", "1")

    ids = _add_done(db, "alpha", 30)
    db.archive_done_tasks(keep_per_project=25)
    archived_ids = {
        t["id"]
        for t in db.get_tasks(project_id="alpha", include_archived=True)
        if t["archived_at"] is not None
    }
    assert len(archived_ids) == 5
    assert archived_ids <= set(ids)

    runner = CliRunner()

    default_view = runner.invoke(pt_cli.tasks_group, ["list", "-p", "alpha", "--all"])
    assert default_view.exit_code == 0, default_view.output
    assert "Total: 25 tasks" in default_view.output

    archived_view = runner.invoke(
        pt_cli.tasks_group, ["list", "-p", "alpha", "--archived"]
    )
    assert archived_view.exit_code == 0, archived_view.output
    assert "Total: 5 tasks" in archived_view.output


# ---------------------------------------------------------------------
# (d) archiving preserves the card
# ---------------------------------------------------------------------


def test_archiving_preserves_status_and_completed_at(tmp_path: Path) -> None:
    db = _setup_db()
    ids = _add_done(db, "alpha", 30)

    before = {
        t["id"]: (t["status"], t["completed_at"], t["updated_at"], t["text"])
        for t in db.get_tasks(project_id="alpha", include_archived=True)
    }
    history_before = db.count_rows("task_history")

    db.archive_done_tasks(keep_per_project=25)

    after = db.get_tasks(project_id="alpha", include_archived=True)
    assert len(after) == 30
    assert {t["id"] for t in after} == set(ids)

    archived = [t for t in after if t["archived_at"] is not None]
    assert len(archived) == 5

    for task in after:
        status, completed_at, updated_at, text = before[task["id"]]
        assert task["status"] == status == "Done"
        assert task["completed_at"] == completed_at
        assert task["updated_at"] == updated_at
        assert task["text"] == text

    # task_history survives too. The hard delete this replaced cascaded into
    # that table and erased its own evidence, so the total is the assertion
    # that matters: archiving must not remove a single history row.
    assert db.count_rows("task_history") == history_before, (
        "archiving changed the task_history row count; #6870 was exactly this "
        "cascade, and it destroyed the record of what had been deleted"
    )


def test_reopening_an_archived_card_unarchives_it(tmp_path: Path) -> None:
    """Done -> archived -> reopened must not make the card vanish.

    `archived_at` is a display cap on *finished* work. The state machine
    allows Done -> Review / In Progress / To Do, so a reopened card that kept
    the flag would sit in an active status and still be filtered out of every
    default board query — gone from Done and gone from Review both. Worse
    than the bug this card fixes, because it hits live work.
    """
    db = _setup_db()
    ids = _add_done(db, "alpha", 30)

    db.archive_done_tasks(keep_per_project=25)
    archived = [
        t
        for t in db.get_tasks(project_id="alpha", include_archived=True)
        if t["archived_at"] is not None
    ]
    assert len(archived) == 5
    reopened_id = archived[0]["id"]
    assert reopened_id not in {t["id"] for t in db.get_tasks(project_id="alpha")}

    updated = db.update_task(reopened_id, status="Review")

    assert updated["status"] == "Review"
    assert updated["archived_at"] is None
    assert updated["completed_at"] is None

    visible = db.get_tasks(project_id="alpha")
    assert reopened_id in {t["id"] for t in visible}
    assert len([t for t in visible if t["status"] == "Review"]) == 1

    # The other four stay archived — reopening one card is not a bulk unarchive.
    still_archived = [
        t
        for t in db.get_tasks(project_id="alpha", include_archived=True)
        if t["archived_at"] is not None
    ]
    assert len(still_archived) == 4
    assert reopened_id not in {t["id"] for t in still_archived}
    assert set(ids) >= {t["id"] for t in still_archived}


def test_archived_at_is_only_ever_set_on_done_cards(tmp_path: Path) -> None:
    """The invariant `archived_at IS NOT NULL` implies `status = 'Done'`,
    enforced in update_task so it holds for every reader, not just get_tasks."""
    db = _setup_db()
    _add_done(db, "alpha", 30)
    db.archive_done_tasks(keep_per_project=25)

    for next_status in ("Review", "In Progress", "To Do"):
        archived = [
            t
            for t in db.get_tasks(project_id="alpha", include_archived=True)
            if t["archived_at"] is not None
        ]
        assert archived, "ran out of archived cards to reopen"
        db.update_task(archived[0]["id"], status=next_status)

    offenders = [
        t
        for t in db.get_tasks(include_archived=True)
        if t["archived_at"] is not None and t["status"] != "Done"
    ]
    assert not offenders, (
        f"{len(offenders)} reopened card(s) kept archived_at and vanished from "
        f"the board: {[t['id'] for t in offenders]}"
    )


def test_archive_rejects_negative_cap(tmp_path: Path) -> None:
    db = _setup_db()
    with pytest.raises(ValueError):
        db.archive_done_tasks(keep_per_project=-1)
