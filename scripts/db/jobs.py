"""Local job listings and submission history for the dashboard."""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from scripts.config import fit_keywords

from .job_ranking import ranked_split


JOB_CATEGORIES = frozenset({
    "Frontend/React", "Full Stack", "Forward Deployed / Solutions",
    "SEO", "Backend", "Other",
})
JOB_SOURCES = frozenset({"ats_sweep", "hn", "manual"})
# 'volunteer' rows live on /jobs/volunteer and never in Jobs listings,
# stats or submissions (#101668605654102016).
JOB_KINDS = frozenset({"job", "volunteer"})

_OPEN = """j.deleted_at IS NULL AND NOT EXISTS (
             SELECT 1 FROM job_submissions AS s WHERE s.job_id = j.id)"""


_REQUIRED_IMPORT_FIELDS = ("company", "title", "url", "source")
_OPTIONAL_TEXT_FIELDS = ("location", "posted_date", "raw")

# The owner, 2026-10-15: Staff/Principal roles stay off the Jobs list. Principal
# and Distinguished are senior wherever they appear, but Staff is a level only
# on the engineering, product, design and science ladders: 'Staff Accountant'
# is entry-level and 'Staff Writer' is no level at all, and remote work of any
# kind stays in (2026-09-23 rule).
_NOT_A_LEVEL_RE = re.compile(
    r"\b(?:member of technical staff|chief of staff|(?:assistant|vice|school)[\s-]principal)\b",
    re.IGNORECASE,
)
_LADDER_ROLE_RE = re.compile(
    r"\b(?:engineers?|developers?|designers?|scientists?|architects?|product managers?|pms?)\b",
    re.IGNORECASE,
)
_SENIOR_TOP_RE = re.compile(r"\b(?:senior|sr\.?)\s+(staff|principal)\b", re.IGNORECASE)
_PRINCIPAL_RE = re.compile(r"\b(?:principal|distinguished)\b", re.IGNORECASE)
_STAFF_RE = re.compile(r"\bstaff\b", re.IGNORECASE)
_LOWER_LEVEL_RE = re.compile(r"\b(?:senior|sr|mid|junior|jr|associate|entry)\b", re.IGNORECASE)


def is_staff_or_principal_only(title: str) -> bool:
    """True when every level the title names is Staff, Principal or Distinguished.

    'Staff/Principal Software Engineer' is left out; 'Senior/Staff Engineer'
    still offers a level the owner takes, so it stays. 'Senior Staff' and 'Sr
    Principal' are one level each. Staff counts only beside a ladder role.
    """
    text = _SENIOR_TOP_RE.sub(r"\1", _NOT_A_LEVEL_RE.sub("", title))
    top = _PRINCIPAL_RE.search(text) or (_STAFF_RE.search(text) and _LADDER_ROLE_RE.search(text))
    return bool(top) and not _LOWER_LEVEL_RE.search(text)


def _listed(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The open jobs the Jobs page shows; its list and category counts agree."""
    return [j for j in jobs if not is_staff_or_principal_only(j["title"])]


def validate_import_record(record: Any) -> dict[str, Any]:
    """Check one decoded JSONL line; return upsert_job kwargs or raise ValueError.

    Every value is type-checked before any set lookup, so a list or object
    where a string belongs rejects only its own line instead of raising
    TypeError out of the whole import.
    """
    if not isinstance(record, dict):
        raise ValueError("line must be a JSON object")
    # Whitespace-only counts as missing, as upsert_job's strip would find it,
    # so a dry run rejects exactly what a real run rejects.
    missing = [
        f for f in _REQUIRED_IMPORT_FIELDS
        if not record.get(f) or (isinstance(record[f], str) and not record[f].strip())
    ]
    if missing:
        raise ValueError(f"missing required fields: {', '.join(missing)}")
    for field in _REQUIRED_IMPORT_FIELDS:
        if not isinstance(record[field], str):
            raise ValueError(f"invalid {field}: {record[field]!r}; must be a string")
    source = record["source"]
    if source not in JOB_SOURCES:
        raise ValueError(f"invalid source: {source!r}; must be one of {sorted(JOB_SOURCES)}")
    category = record.get("category", "Other")
    if not isinstance(category, str) or category not in JOB_CATEGORIES:
        raise ValueError(
            f"invalid category: {category!r}; must be one of {sorted(JOB_CATEGORIES)}"
        )
    # Omitted kind means "default / keep the row's kind"; an explicit value,
    # null included, must be one of JOB_KINDS (`pt jobs import --help`).
    kind = record.get("kind")
    if "kind" in record and (not isinstance(kind, str) or kind not in JOB_KINDS):
        raise ValueError(f"invalid kind: {kind!r}; must be one of {sorted(JOB_KINDS)}")
    for field in _OPTIONAL_TEXT_FIELDS:
        value = record.get(field)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"invalid {field}: {value!r}; must be a string or null")
    return {
        "company": record["company"], "title": record["title"], "url": record["url"],
        "source": source, "category": category, "kind": kind,
        **{field: record.get(field) for field in _OPTIONAL_TEXT_FIELDS},
    }


class JobsMigrationPendingError(ValueError):
    """A volunteer operation on a database still waiting for migration 016."""

    def __init__(self) -> None:
        super().__init__("volunteer roles need migration 016; run `pt db migrate`")


def _has_kind_columns(conn: Any) -> bool:
    """True once migration 016 has added jobs.kind and jobs.volunteering_since.

    Numbered migrations reach an existing database only through
    `pt db migrate`, which takes a verified backup first; neither the
    dashboard nor `pt` applies them at startup. Until an operator runs it,
    every row is a job and none has a volunteering date, so the Jobs page
    keeps working and the Volunteer page is empty instead of a 500.
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    return {"kind", "volunteering_since"} <= columns


def _kind_sql(conn: Any) -> tuple[str, str]:
    """SQL for ``j.kind`` and ``j.volunteering_since``, as literals before 016."""
    if _has_kind_columns(conn):
        return "j.kind", "j.volunteering_since"
    return "'job'", "NULL"


def _open_rows(conn: Any, kind: str) -> list[dict[str, Any]]:
    """Open listings of one kind on ``conn``, newest first.

    A volunteer role with volunteering_since is Current, not Possible; a
    job is listed whatever that column holds, so no job can fall off
    both pages.
    """
    kind_sql, since_sql = _kind_sql(conn)
    since_filter = f"AND {since_sql} IS NULL" if kind == "volunteer" else ""
    rows = conn.execute(
        f"""SELECT j.* FROM jobs AS j
            WHERE {kind_sql} = ? {since_filter} AND {_OPEN}
            ORDER BY j.first_seen DESC, j.id DESC""",
        (kind,),
    ).fetchall()
    return [dict(row) for row in rows]


class JobsMixin:
    """Job operations exposed through the shared DatabaseManager."""

    def upsert_job(
        self, *, company: str, title: str, url: str, source: str,
        location: str | None = None, posted_date: str | None = None,
        category: str = "Other", raw: str | None = None,
        kind: str | None = None,
    ) -> dict[str, Any]:
        """Update a URL's listing without changing first-seen or dismissal state.

        ``kind`` defaults to 'job' for a new row; omitting it on a later
        import keeps whatever kind the row already has. A row that ends up
        a job loses any volunteering_since, which only volunteer roles carry.
        """
        if not all(isinstance(value, str) for value in (company, title, url)):
            raise ValueError("company, title and url must be strings")
        company, title, url = company.strip(), title.strip(), url.strip()
        if not company or not title or not url:
            raise ValueError("company, title and url are required")
        # isinstance first: an unhashable value must be a ValueError, not a
        # TypeError from the set lookup.
        if not isinstance(source, str) or source not in JOB_SOURCES:
            raise ValueError("invalid job source")
        if not isinstance(category, str) or category not in JOB_CATEGORIES:
            raise ValueError("invalid job category")
        if kind is not None and (not isinstance(kind, str) or kind not in JOB_KINDS):
            raise ValueError("invalid job kind")
        first_seen = datetime.now(timezone.utc).date().isoformat()
        with self._get_conn() as conn:
            migrated = _has_kind_columns(conn)
            if not migrated and kind == "volunteer":
                raise JobsMigrationPendingError()
            kind_column = ", kind" if migrated else ""
            kind_value = ", COALESCE(?, 'job')" if migrated else ""
            # SET expressions read the pre-update row, so jobs.kind here is
            # the kind before this import.
            kind_update = (
                """, kind=COALESCE(?, jobs.kind),
                     volunteering_since=CASE WHEN COALESCE(?, jobs.kind) = 'job'
                       THEN NULL ELSE jobs.volunteering_since END"""
                if migrated else ""
            )
            conn.execute(
                f"""INSERT INTO jobs
                   (company, title, location, url, source, posted_date,
                    first_seen, category, raw{kind_column})
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?{kind_value})
                   ON CONFLICT(url) DO UPDATE SET
                     company=excluded.company, title=excluded.title,
                     location=COALESCE(excluded.location, jobs.location),
                     source=excluded.source,
                     posted_date=COALESCE(excluded.posted_date, jobs.posted_date),
                     category=excluded.category,
                     raw=COALESCE(excluded.raw, jobs.raw){kind_update}""",
                (company, title, location, url, source, posted_date,
                 first_seen, category, raw) + ((kind, kind, kind) if migrated else ()),
            )
            row = conn.execute("SELECT * FROM jobs WHERE url = ?", (url,)).fetchone()
            conn.commit()
            return dict(row)

    def volunteer_kind_supported(self) -> bool:
        """False until migration 016 adds jobs.kind (see _has_kind_columns)."""
        with self._get_conn() as conn:
            return _has_kind_columns(conn)

    def get_open_jobs(self, kind: str = "job") -> list[dict[str, Any]]:
        """Listings of one kind neither dismissed nor submitted, newest first."""
        with self._get_conn() as conn:
            return _open_rows(conn, kind)

    def get_ranked_open_jobs(self, kind: str = "job") -> dict[str, list[dict[str, Any]]]:
        """Open listings ranked by fit, split into fresh and stale (#101679546294759424)."""
        today = datetime.now(timezone.utc).date()
        jobs = self.get_open_jobs(kind)
        return ranked_split(_listed(jobs) if kind == "job" else jobs, today, fit_keywords())

    def get_volunteer_roles(self) -> dict[str, Any]:
        """The Volunteer page: current, applied, and ranked possible roles.

        A role with volunteering_since is Current whether or not it was ever
        submitted; Applied and Possible both exclude it. All sections come
        from one read transaction, so a write from another tab can never land
        between them and leave a role in no section (or two).
        """
        keywords = fit_keywords()  # read before the snapshot opens
        with self._get_conn() as conn:
            conn.execute("BEGIN")  # one WAL snapshot for every query below
            try:
                return self._volunteer_sections(conn, keywords)
            finally:
                conn.rollback()  # read-only: nothing to keep

    def _volunteer_sections(self, conn: Any, keywords: tuple[str, ...]) -> dict[str, Any]:
        """current/applied/possible on ``conn``, inside the caller's read transaction."""
        migration_pending = not _has_kind_columns(conn)
        kind_sql, since_sql = _kind_sql(conn)
        current = conn.execute(
            f"""SELECT j.* FROM jobs AS j
                WHERE {kind_sql} = 'volunteer' AND j.deleted_at IS NULL
                  AND {since_sql} IS NOT NULL
                ORDER BY {since_sql} DESC, j.id DESC"""
        ).fetchall()
        applied = conn.execute(
            f"""SELECT j.*, MAX(s.submitted_at) AS last_submitted_at
                FROM jobs AS j JOIN job_submissions AS s ON s.job_id = j.id
                WHERE {kind_sql} = 'volunteer' AND j.deleted_at IS NULL
                  AND {since_sql} IS NULL
                GROUP BY j.id ORDER BY last_submitted_at DESC, j.id DESC"""
        ).fetchall()
        possible = ranked_split(_open_rows(conn, "volunteer"), datetime.now(timezone.utc).date(), keywords)
        return {
            "migration_pending": migration_pending,
            "current": [dict(row) for row in current],
            "applied": [dict(row) for row in applied],
            "possible": possible["fresh"],
            "possible_stale": possible["stale"],
        }

    def start_volunteering(self, job_id: int) -> dict[str, Any] | None:
        """Stamp volunteering_since once; None for a missing or dismissed volunteer role.

        Raises JobsMigrationPendingError before migration 016.
        """
        with self._get_conn() as conn:
            if not _has_kind_columns(conn):
                raise JobsMigrationPendingError()
            conn.execute(
                """UPDATE jobs SET volunteering_since = ?
                   WHERE id = ? AND kind = 'volunteer' AND deleted_at IS NULL
                     AND volunteering_since IS NULL""",
                (datetime.now(timezone.utc).isoformat(), job_id),
            )
            row = conn.execute(
                """SELECT * FROM jobs
                   WHERE id = ? AND kind = 'volunteer' AND deleted_at IS NULL""",
                (job_id,),
            ).fetchone()
            conn.commit()
            return dict(row) if row else None

    def soft_delete_job(self, job_id: int) -> dict[str, Any] | None:
        """Dismiss a listing while keeping its row and submission history."""
        with self._get_conn() as conn:
            cursor = conn.execute(
                """UPDATE jobs SET deleted_at = ?
                   WHERE id = ? AND deleted_at IS NULL""",
                (datetime.now(timezone.utc).isoformat(), job_id),
            )
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            conn.commit()
            return dict(row) if cursor.rowcount and row else None

    def add_job_submission(
        self, job_id: int, *, submitted_at: str | None = None,
        resume_path: str | None = None, cover_letter_path: str | None = None,
        notes: str | None = None,
    ) -> dict[str, Any] | None:
        """Append one submission; a later submission never replaces an earlier one."""
        supplied = submitted_at or datetime.now(timezone.utc).isoformat()
        try:
            parsed = datetime.fromisoformat(supplied.replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid submitted_at date") from exc
        # A date-only historical entry is midnight UTC; explicit offsets are
        # converted before storage so text ordering matches chronological order.
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        timestamp = parsed.astimezone(timezone.utc).isoformat()
        with self._get_conn() as conn:
            job = conn.execute(
                "SELECT id FROM jobs WHERE id = ? AND deleted_at IS NULL", (job_id,)
            ).fetchone()
            if job is None:
                return None
            cursor = conn.execute(
                """INSERT INTO job_submissions
                   (job_id, submitted_at, resume_path, cover_letter_path, notes)
                   VALUES (?, ?, ?, ?, ?)""",
                (job_id, timestamp, resume_path, cover_letter_path, notes),
            )
            row = conn.execute(
                "SELECT * FROM job_submissions WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
            conn.commit()
            return dict(row)

    def get_submitted_jobs(self) -> list[dict[str, Any]]:
        """Group every submission under its listing, latest activity first."""
        with self._get_conn() as conn:
            kind_sql, _ = _kind_sql(conn)
            rows = conn.execute(
                f"""SELECT j.*, s.id AS submission_id, s.submitted_at,
                          s.resume_path, s.cover_letter_path, s.notes
                   FROM jobs AS j JOIN job_submissions AS s ON s.job_id = j.id
                   WHERE {kind_sql} = 'job'
                   ORDER BY s.submitted_at DESC, s.id DESC"""
            ).fetchall()
        groups: dict[int, dict[str, Any]] = {}
        for row in rows:
            record = dict(row)
            job_id = record["id"]
            if job_id not in groups:
                groups[job_id] = {
                    "job": {key: record[key] for key in (
                        "id", "company", "title", "location", "url", "source",
                        "posted_date", "first_seen", "category", "raw", "deleted_at",
                    )},
                    "submissions": [],
                }
            groups[job_id]["submissions"].append({
                "id": record["submission_id"], "job_id": job_id,
                "submitted_at": record["submitted_at"],
                "resume_path": record["resume_path"],
                "cover_letter_path": record["cover_letter_path"],
                "notes": record["notes"],
            })
        return list(groups.values())

    def get_job_stats(self) -> dict[str, list[dict[str, Any]]]:
        """Discovery/submission history and current open category counts."""
        with self._get_conn() as conn:
            kind_sql, _ = _kind_sql(conn)
            jobs = conn.execute(
                f"""SELECT j.first_seen AS date, COUNT(*) AS count FROM jobs AS j
                    WHERE {kind_sql} = 'job' GROUP BY j.first_seen ORDER BY j.first_seen"""
            ).fetchall()
            submissions = conn.execute(
                f"""SELECT substr(s.submitted_at, 1, 10) AS date, COUNT(*) AS count
                   FROM job_submissions AS s JOIN jobs AS j ON j.id = s.job_id
                   WHERE {kind_sql} = 'job'
                   GROUP BY substr(s.submitted_at, 1, 10) ORDER BY date"""
            ).fetchall()
        counts = Counter(j["category"] for j in _listed(self.get_open_jobs("job")))
        return {
            "jobs_per_day": [dict(row) for row in jobs],
            "submissions_per_day": [dict(row) for row in submissions],
            "categories": [
                {"category": c, "count": n}
                for c, n in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
            ],
        }
