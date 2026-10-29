"""Job import from JSONL: idempotency, raw retention, multi-role contract."""

import json
import subprocess
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile

import pytest

from scripts.db.jobs import JOB_CATEGORIES, JOB_SOURCES


def test_import_upsert_is_idempotent_by_url(db, tmp_path: Path) -> None:
    """Repeated imports of the same URL update fields but preserve first_seen."""
    jsonl = tmp_path / "jobs.jsonl"
    jsonl.write_text(
        '{"company": "Acme", "title": "Engineer", "url": "https://example.test/1", "source": "ats_sweep"}\n'
        '{"company": "Widget", "title": "Designer", "url": "https://example.test/2", "source": "hn"}\n'
    )
    
    # First import
    for line in jsonl.read_text().strip().split("\n"):
        import json
        record = json.loads(line)
        db.upsert_job(**record)
    
    first_jobs = db.get_open_jobs()
    assert len(first_jobs) == 2
    first_seen_by_url = {job["url"]: job["first_seen"] for job in first_jobs}
    
    # Update one job's title and re-import
    jsonl.write_text(
        '{"company": "Acme Corp", "title": "Senior Engineer", "url": "https://example.test/1", "source": "ats_sweep", "category": "Backend"}\n'
        '{"company": "Widget", "title": "Designer", "url": "https://example.test/2", "source": "hn"}\n'
    )
    
    for line in jsonl.read_text().strip().split("\n"):
        import json
        record = json.loads(line)
        db.upsert_job(**record)
    
    second_jobs = db.get_open_jobs()
    assert len(second_jobs) == 2
    
    # URL 1 should have updated fields but preserved first_seen
    updated = next(j for j in second_jobs if j["url"] == "https://example.test/1")
    assert updated["company"] == "Acme Corp"
    assert updated["title"] == "Senior Engineer"
    assert updated["category"] == "Backend"
    assert updated["first_seen"] == first_seen_by_url["https://example.test/1"]
    
    # URL 2 unchanged
    unchanged = next(j for j in second_jobs if j["url"] == "https://example.test/2")
    assert unchanged["company"] == "Widget"
    assert unchanged["first_seen"] == first_seen_by_url["https://example.test/2"]


def test_import_preserves_raw_text_and_dismissal_state(db, tmp_path: Path) -> None:
    """Re-importing a dismissed job preserves deleted_at and raw text."""
    jsonl = tmp_path / "jobs.jsonl"
    initial_raw = "Original ATS posting HTML with full details"
    jsonl.write_text(
        f'{{"company": "Example", "title": "Role", "url": "https://example.test/job", '
        f'"source": "ats_sweep", "raw": "{initial_raw}"}}\n'
    )
    
    import json
    job = db.upsert_job(**json.loads(jsonl.read_text().strip()))
    assert job["raw"] == initial_raw
    assert job["deleted_at"] is None
    
    # Dismiss the job
    dismissed = db.soft_delete_job(job["id"])
    assert dismissed["deleted_at"] is not None
    
    # Re-import with updated fields but no raw
    jsonl.write_text(
        '{"company": "Example Inc", "title": "Updated Role", "url": "https://example.test/job", '
        '"source": "ats_sweep"}\n'
    )
    reimported = db.upsert_job(**json.loads(jsonl.read_text().strip()))
    
    # Should preserve dismissal and raw text
    assert reimported["id"] == job["id"]
    assert reimported["deleted_at"] is not None
    assert reimported["raw"] == initial_raw
    assert reimported["company"] == "Example Inc"


def test_import_validates_required_fields(tmp_path: Path) -> None:
    """Missing required fields raise ValueError."""
    from scripts.db.manager import DatabaseManager
    
    db = DatabaseManager()
    
    # Missing company
    with pytest.raises(ValueError, match="company, title and url are required"):
        db.upsert_job(company="", title="Engineer", url="https://example.test/1", source="manual")
    
    # Missing title
    with pytest.raises(ValueError, match="company, title and url are required"):
        db.upsert_job(company="Acme", title="", url="https://example.test/2", source="manual")
    
    # Missing URL
    with pytest.raises(ValueError, match="company, title and url are required"):
        db.upsert_job(company="Acme", title="Engineer", url="", source="manual")
    
    # Invalid source
    with pytest.raises(ValueError, match="invalid job source"):
        db.upsert_job(company="Acme", title="Engineer", url="https://example.test/3", source="linkedin")
    
    # Invalid category
    with pytest.raises(ValueError, match="invalid job category"):
        db.upsert_job(
            company="Acme", title="Engineer", url="https://example.test/4",
            source="manual", category="Crypto"
        )


def _import_help() -> str:
    """`pt jobs import --help`: the import contract exporters follow."""
    result = subprocess.run(
        [sys.executable, "-m", "scripts.pt", "jobs", "import", "--help"],
        capture_output=True, text=True, cwd=Path(__file__).parent.parent, timeout=60,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    # Click re-wraps paragraphs; compare on single spaces.
    return " ".join(result.stdout.split())


def test_hn_multi_role_contract_documented_in_import_help() -> None:
    """HN comments guarantee one #post row; #role-N rows are best-effort.

    Free-text HN headers can't be parsed completely, so the contract
    guarantees one {comment_url}#post row per comment carrying the full
    comment text (no role is lost), and treats #role-N rows as extras.
    The export side lives in the exporter's repo; the contract is the
    `pt jobs import --help` text.
    """
    contract = _import_help()
    assert "{comment_url}#post" in contract
    assert "full comment text" in contract.lower()
    assert "#role-{N}" in contract
    assert "best-effort" in contract.lower()
    assert "Hacker News (company not parsed)" in contract
    assert "Full HN post (read for every role)" in contract
    assert '"Unknown"' not in contract  # retired label (exporter PR #20)
    assert '"See raw text"' not in contract
    assert "Omitting it, or cutting its raw text, breaks the contract" in contract
    assert "Every advertised position must produce one JSONL line" not in contract


def test_hn_post_and_role_rows_import_as_separate_jobs(db, tmp_path: Path) -> None:
    """A comment's guaranteed #post row and its best-effort #role-N rows are distinct jobs."""
    jsonl = tmp_path / "hn_multi.jsonl"
    jsonl.write_text(
        '{"company": "Widget Inc", "title": "Full HN post (read for every role)", '
        '"url": "https://news.ycombinator.com/item?id=12345678#post", '
        '"source": "hn", "category": "Other", '
        '"raw": "Widget Inc | Full Stack, Backend | Remote | Apply at jobs@widget"}\n'

        '{"company": "Widget Inc", "title": "Full Stack Developer", '
        '"url": "https://news.ycombinator.com/item?id=12345678#role-1", '
        '"source": "hn", "category": "Full Stack", '
        '"raw": "Widget Inc | Full Stack, Backend | Remote | Apply at jobs@widget"}\n'

        '{"company": "Hacker News (company not parsed)", "title": "Full HN post (read for every role)", '
        '"url": "https://news.ycombinator.com/item?id=87654321#post", '
        '"source": "hn", "category": "Other", '
        '"raw": "Stealth startup hiring several engineers, contact: secret@example"}\n'
    )

    import json
    imported = []
    for line in jsonl.read_text().strip().split("\n"):
        record = json.loads(line)
        job = db.upsert_job(**record)
        imported.append(job)

    assert [job["url"] for job in imported] == [
        "https://news.ycombinator.com/item?id=12345678#post",
        "https://news.ycombinator.com/item?id=12345678#role-1",
        "https://news.ycombinator.com/item?id=87654321#post",
    ]
    assert len({job["id"] for job in imported}) == 3

    # The #post row keeps the whole comment even though only one role parsed.
    assert "Full Stack, Backend" in imported[0]["raw"]

    # An unparseable comment still arrives through its #post row.
    unknown = imported[2]
    assert unknown["company"] == "Hacker News (company not parsed)"
    assert unknown["title"] == "Full HN post (read for every role)"
    assert "Stealth startup" in unknown["raw"]
    
    # All have raw text
    assert all(job["raw"] for job in imported)


def test_valid_sources_and_categories_constants() -> None:
    """JOB_SOURCES and JOB_CATEGORIES match schema constraints."""
    assert JOB_SOURCES == frozenset({"ats_sweep", "hn", "manual"})
    assert JOB_CATEGORIES == frozenset({
        "Frontend/React", "Full Stack", "Forward Deployed / Solutions",
        "SEO", "Backend", "Other",
    })


def test_import_coalesce_behavior_for_optional_fields(db) -> None:
    """Upsert COALESCE logic: new null values don't erase existing data."""
    # First import with all fields populated
    first = db.upsert_job(
        company="Acme", title="Engineer", url="https://example.test/coalesce",
        source="ats_sweep", location="Remote", posted_date="2026-09-20",
        category="Backend", raw="Initial posting text"
    )
    assert first["location"] == "Remote"
    assert first["posted_date"] == "2026-09-20"
    assert first["raw"] == "Initial posting text"
    
    # Re-import with nulls for optional fields
    second = db.upsert_job(
        company="Acme Corp", title="Senior Engineer",
        url="https://example.test/coalesce", source="ats_sweep",
        location=None, posted_date=None, raw=None,
    )
    
    # COALESCE preserves existing values when new value is null
    assert second["id"] == first["id"]
    assert second["company"] == "Acme Corp"
    assert second["title"] == "Senior Engineer"
    assert second["location"] == "Remote"
    assert second["posted_date"] == "2026-09-20"
    assert second["raw"] == "Initial posting text"


def test_cli_import_success_output(tmp_path: Path) -> None:
    """End-to-end CLI test: successful import shows correct output."""
    jsonl = tmp_path / "success.jsonl"
    jsonl.write_text(
        '{"company": "Test Co", "title": "Engineer", "url": "https://example.test/cli1", "source": "manual"}\n'
    )
    
    result = subprocess.run(
        [sys.executable, "-m", "scripts.pt", "jobs", "import", str(jsonl)],
        capture_output=True, text=True, cwd=Path(__file__).parent.parent
    )
    
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert "✓ Imported 1 job(s)" in result.stdout
    assert str(jsonl) in result.stdout


def test_cli_import_json_output(tmp_path: Path) -> None:
    """End-to-end CLI test: --json flag produces valid JSON envelope."""
    jsonl = tmp_path / "json_mode.jsonl"
    jsonl.write_text(
        '{"company": "Widget", "title": "Designer", "url": "https://example.test/cli2", "source": "hn"}\n'
    )
    
    result = subprocess.run(
        [sys.executable, "-m", "scripts.pt", "jobs", "import", str(jsonl), "--json"],
        capture_output=True, text=True, cwd=Path(__file__).parent.parent
    )
    
    assert result.returncode == 0, f"stderr: {result.stderr}"
    response = json.loads(result.stdout)
    assert response["ok"] is True
    assert response["command"] == "jobs.import"
    assert response["result"]["imported"] == 1
    assert response["result"]["errors"] == 0


def test_cli_import_malformed_jsonl_error(tmp_path: Path) -> None:
    """End-to-end CLI test: malformed JSONL produces error messages."""
    jsonl = tmp_path / "malformed.jsonl"
    jsonl.write_text(
        '{"company": "Good", "title": "Engineer", "url": "https://example.test/cli3", "source": "manual"}\n'
        '{broken json here}\n'
        '{"company": "Also Good", "title": "Designer", "url": "https://example.test/cli4", "source": "hn"}\n'
    )
    
    result = subprocess.run(
        [sys.executable, "-m", "scripts.pt", "jobs", "import", str(jsonl)],
        capture_output=True, text=True, cwd=Path(__file__).parent.parent
    )
    
    assert result.returncode == 1
    assert "✓ Imported 2 job(s)" in result.stdout
    assert "✗ 1 error(s):" in result.stderr
    assert "line 2:" in result.stderr
    assert "invalid JSON" in result.stderr


def test_cli_import_missing_required_fields_error(tmp_path: Path) -> None:
    """End-to-end CLI test: missing required fields consolidated into one error."""
    jsonl = tmp_path / "missing_fields.jsonl"
    jsonl.write_text(
        '{"title": "Engineer", "url": "https://example.test/cli5"}\n'
        '{"company": "Widget"}\n'
    )
    
    result = subprocess.run(
        [sys.executable, "-m", "scripts.pt", "jobs", "import", str(jsonl)],
        capture_output=True, text=True, cwd=Path(__file__).parent.parent
    )
    
    assert result.returncode == 1
    assert "✗ 2 error(s):" in result.stderr
    assert "line 1:" in result.stderr
    assert "missing required fields:" in result.stderr
    assert "company" in result.stderr and "source" in result.stderr
    assert "line 2:" in result.stderr


def test_cli_import_many_errors_truncated(tmp_path: Path) -> None:
    """End-to-end CLI test: more than 10 errors triggers truncation message."""
    jsonl = tmp_path / "many_errors.jsonl"
    lines = ['{broken}' for _ in range(15)]
    jsonl.write_text('\n'.join(lines) + '\n')
    
    result = subprocess.run(
        [sys.executable, "-m", "scripts.pt", "jobs", "import", str(jsonl)],
        capture_output=True, text=True, cwd=Path(__file__).parent.parent
    )
    
    assert result.returncode == 1
    assert "✗ 15 error(s):" in result.stderr
    assert "... and 5 more error(s)" in result.stderr


def test_cli_import_nonexistent_file_error(tmp_path: Path) -> None:
    """End-to-end CLI test: nonexistent file produces IO error."""
    nonexistent = tmp_path / "does_not_exist.jsonl"
    
    result = subprocess.run(
        [sys.executable, "-m", "scripts.pt", "jobs", "import", str(nonexistent)],
        capture_output=True, text=True, cwd=Path(__file__).parent.parent
    )
    
    assert result.returncode == 2
    assert "does not exist" in result.stderr.lower() or "no such file" in result.stderr.lower()


def test_cli_import_dry_run_mode(tmp_path: Path) -> None:
    """End-to-end CLI test: --dry-run validates without writing to database."""
    jsonl = tmp_path / "dry_run.jsonl"
    jsonl.write_text(
        '{"company": "Dry Run Co", "title": "Tester", "url": "https://example.test/dry", "source": "manual"}\n'
    )
    
    result = subprocess.run(
        [sys.executable, "-m", "scripts.pt", "jobs", "import", str(jsonl), "--dry-run"],
        capture_output=True, text=True, cwd=Path(__file__).parent.parent
    )
    
    assert result.returncode == 0
    assert "DRY RUN: Would import 1 job(s)" in result.stdout


def _run_import_json(jsonl: Path, *extra: str) -> subprocess.CompletedProcess:
    """`pt jobs import --json`, bounded; JSON mode reports line errors with exit 0."""
    result = subprocess.run(
        [sys.executable, "-m", "scripts.pt", "jobs", "import", str(jsonl), "--json", *extra],
        capture_output=True, text=True, cwd=Path(__file__).parent.parent, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return result


def test_cli_import_kind_and_rejects_unknown_kind(db, tmp_path: Path) -> None:
    """kind is optional (default 'job'); 'volunteer' lands on the Volunteer page; others are rejected."""
    jsonl = tmp_path / "kinds.jsonl"
    jsonl.write_text(
        '{"company": "Acme", "title": "Engineer", "url": "https://example.test/k1", "source": "manual"}\n'
        '{"company": "Food Bank", "title": "Website help", "url": "https://example.test/k2", "source": "manual", "kind": "volunteer"}\n'
        '{"company": "Gig Co", "title": "Driver", "url": "https://example.test/k3", "source": "manual", "kind": "gig"}\n'
    )
    result = _run_import_json(jsonl)
    response = json.loads(result.stdout)
    assert response["ok"] is False
    assert response["result"]["imported"] == 2
    [error] = response["result"]["details"]["errors"]
    assert error["line"] == 3 and "invalid kind" in error["reason"]

    assert [job["url"] for job in db.get_open_jobs()] == ["https://example.test/k1"]
    assert [job["url"] for job in db.get_open_jobs("volunteer")] == ["https://example.test/k2"]
    assert "https://example.test/k3" not in {
        job["url"] for kind in ("job", "volunteer") for job in db.get_open_jobs(kind)
    }


def test_kind_and_null_rules_documented_in_import_help() -> None:
    contract = _import_help()
    assert "kind job (default for a new row) | volunteer" in contract
    assert "an explicit null" in contract
    assert "/jobs/volunteer" in contract
    # Matches _run_import_json (exit 0) and the exit-1 tests for plain mode.
    assert 'The command then exits 1; with --json it exits 0 and the envelope says "ok": false.' in contract


_GOOD = {"company": "Acme", "title": "Engineer", "source": "manual"}

# Each bad line must reject only itself; a valid line after it still imports.
_BAD_LINES = [
    ("not an object", [1, 2], "must be a JSON object"),
    ("number line", 5, "must be a JSON object"),
    ("company list", {**_GOOD, "company": ["Acme"]}, "invalid company"),
    ("title number", {**_GOOD, "title": 42}, "invalid title"),
    ("url object", {**_GOOD, "url": {"href": "x"}}, "invalid url"),
    ("source object", {**_GOOD, "source": {"hn": 1}}, "invalid source"),
    ("source unknown", {**_GOOD, "source": "linkedin"}, "invalid source"),
    ("category list", {**_GOOD, "category": ["SEO"]}, "invalid category"),
    ("category null", {**_GOOD, "category": None}, "invalid category"),
    ("kind list", {**_GOOD, "kind": []}, "invalid kind"),
    ("kind null", {**_GOOD, "kind": None}, "invalid kind"),
    ("kind empty", {**_GOOD, "kind": ""}, "invalid kind"),
    ("source null", {**_GOOD, "source": None}, "missing required fields: source"),
    ("title null", {**_GOOD, "title": None}, "missing required fields: title"),
    ("company blank", {**_GOOD, "company": "   "}, "missing required fields: company"),
    ("title blank", {**_GOOD, "title": "\t "}, "missing required fields: title"),
    ("url blank", {**_GOOD, "url": "  "}, "missing required fields: url"),
    ("kind object", {**_GOOD, "kind": {"a": 1}}, "invalid kind"),
    ("kind unknown", {**_GOOD, "kind": "gig"}, "invalid kind"),
    ("location list", {**_GOOD, "location": ["Remote"]}, "invalid location"),
    ("posted_date number", {**_GOOD, "posted_date": 20261001}, "invalid posted_date"),
    ("raw object", {**_GOOD, "raw": {"html": "<p>"}}, "invalid raw"),
    ("missing company", {k: v for k, v in _GOOD.items() if k != "company"}, "missing required fields: company"),
]


@pytest.mark.parametrize("dry_run", [False, True], ids=["real", "dry-run"])
@pytest.mark.parametrize("label,bad,reason", _BAD_LINES, ids=[row[0] for row in _BAD_LINES])
def test_cli_import_rejects_only_the_bad_line(
    db, tmp_path: Path, label: str, bad, reason: str, dry_run: bool,
) -> None:
    """A dry run reports the same line error a real run gives (rule E1)."""
    if isinstance(bad, dict) and "company" in bad:
        bad = {**bad, "url": bad.get("url", "https://example.test/bad")}
    good = {**_GOOD, "url": "https://example.test/good", "kind": "volunteer",
            "location": None, "posted_date": "2026-10-01", "raw": "text"}
    jsonl = tmp_path / "mixed.jsonl"
    jsonl.write_text(json.dumps(bad) + "\n" + json.dumps(good) + "\n")

    response = json.loads(_run_import_json(jsonl, *(["--dry-run"] if dry_run else [])).stdout)
    assert response["ok"] is False
    assert response["result"]["imported"] == 1
    assert response["result"]["dry_run"] is dry_run
    [error] = response["result"]["details"]["errors"]
    assert error["line"] == 1 and reason in error["reason"], error
    expected = [] if dry_run else ["https://example.test/good"]
    assert [job["url"] for job in db.get_open_jobs("volunteer")] == expected
    assert db.get_open_jobs() == []


def test_cli_import_omitted_kind_defaults_then_keeps_existing_kind(db, tmp_path: Path) -> None:
    """Omitted kind: new row is a job; on conflict the row keeps its kind. Null is an error."""
    jsonl = tmp_path / "omitted.jsonl"
    new_job = {**_GOOD, "url": "https://example.test/omit/job"}
    volunteer = {**_GOOD, "url": "https://example.test/omit/vol", "kind": "volunteer"}
    jsonl.write_text(json.dumps(new_job) + "\n" + json.dumps(volunteer) + "\n")
    assert json.loads(_run_import_json(jsonl).stdout)["ok"] is True

    # Re-import the volunteer row without kind (kept), then with kind null (rejected).
    refresh = {k: v for k, v in volunteer.items() if k != "kind"}
    jsonl.write_text(json.dumps({**refresh, "title": "Renamed"}) + "\n"
                     + json.dumps({**refresh, "kind": None}) + "\n")
    response = json.loads(_run_import_json(jsonl).stdout)
    assert response["result"]["imported"] == 1
    [error] = response["result"]["details"]["errors"]
    assert error["line"] == 2 and "invalid kind" in error["reason"]

    assert [job["url"] for job in db.get_open_jobs()] == ["https://example.test/omit/job"]
    [kept] = db.get_open_jobs("volunteer")
    assert kept["url"] == "https://example.test/omit/vol" and kept["title"] == "Renamed"
