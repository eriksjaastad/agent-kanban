"""Property-based tests for Kanban board database operations.

Feature: kanban-board
Properties tested:
- Property 1: Task Creation Completeness
- Property 2: Query Filtering Accuracy
- Property 12: Secret Pattern Detection
- Property 16: Project Validation
"""

import pytest
import sqlite3
from contextlib import contextmanager
import tempfile
import re
from pathlib import Path
from datetime import datetime

from hypothesis import given, strategies as st, assume, settings

# Every property test here runs real SQLite I/O per example, so Hypothesis's
# default 200ms per-example deadline measured disk and machine load, not this
# code: under a loaded machine they failed with DeadlineExceeded at 260-507ms
# on an unchanged tree (#7896). The tests check behaviour, not speed.
DB_PROPERTY_SETTINGS = {"deadline": None}

# These property tests use isolated temp databases

# Add scripts to path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from db.backend_manager import DatabaseManager as BackendDatabaseManager
from db.schema import create_database


# Secret patterns from design.md
SECRET_PATTERNS = [
    r'sk-[a-zA-Z0-9]{32,}',           # OpenAI keys
    r'api[_-]?key[_-]?=\s*["\']?[\w-]+',  # Generic API keys
    r'\d{3}-\d{2}-\d{4}',             # SSN format
    r'[A-Z0-9]{20,}',                 # Generic long tokens
    r'password\s*=\s*["\']?[\w-]+',   # Passwords
]


def contains_secret(text: str) -> tuple[bool, str]:
    """Check if text contains secret patterns.
    
    Returns:
        (is_secret, pattern_matched)
    """
    for pattern in SECRET_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return (True, pattern)
    return (False, "")


@contextmanager
def _fresh_database():
    """Yield (db_path, db_manager, project_ids) for a fresh database in its own
    temporary directory, which is removed afterwards with any backups it got.
    Hypothesis runs many examples per test, so pytest's tmp_path can't do this.
    """
    with tempfile.TemporaryDirectory() as tmp:
        yield _build_database(Path(tmp) / "test.db")


def _build_database(db_path: Path):
    # Create database schema
    create_database(db_path)
    
    # Add tasks and task_history tables (from design.md)
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('Backlog', 'To Do', 'In Progress', 'Done')),
            project_id TEXT NOT NULL,
            priority TEXT CHECK(priority IN ('Critical', 'High', 'Medium', 'Low', NULL)),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS task_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            project_id TEXT NOT NULL,
            event_type TEXT NOT NULL CHECK(event_type IN ('created', 'status_changed', 'completed')),
            old_status TEXT,
            new_status TEXT,
            timestamp TEXT NOT NULL,
            FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
        )
    """)
    
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_tasks_completed ON tasks(completed_at) WHERE completed_at IS NOT NULL")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_history_timestamp ON task_history(timestamp)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_history_project ON task_history(project_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_history_event ON task_history(event_type)")
    
    conn.commit()
    conn.close()
    
    # Create DatabaseManager instance
    db_manager = BackendDatabaseManager(db_path)
    
    # Create test projects
    projects = [
        ("project-tracker", "Project Tracker", "/path/to/project-tracker"),
        ("image-workflow", "Image Workflow", "/path/to/image-workflow"),
        ("trading-desk", "Trading Desk", "/path/to/trading-desk"),
        ("test-project-1", "Test Project 1", "/path/to/test-project-1"),
        ("test-project-2", "Test Project 2", "/path/to/test-project-2"),
    ]
    
    project_ids = []
    for project_id, name, path in projects:
        db_manager.add_project(
            project_id=project_id,
            name=name,
            path=path,
            status="active"
        )
        project_ids.append(project_id)
    
    return db_path, db_manager, project_ids


# Property 1: Task Creation Completeness
# For any valid task data, creating a task should result in a task object
# containing all provided fields plus generated fields (id, created_at, updated_at),
# and querying that task by ID should return the same data
@given(
    text=st.text(min_size=1, max_size=1000).map(lambda s: s.strip()).filter(lambda s: len(s) > 0),
    project_id=st.sampled_from(["project-tracker", "image-workflow", "trading-desk", "test-project-1", "test-project-2"]),
    status=st.sampled_from(["Backlog", "To Do", "In Progress", "Done"]),
    priority=st.one_of(st.none(), st.sampled_from(["Critical", "High", "Medium", "Low"]))
)
@settings(max_examples=100, **DB_PROPERTY_SETTINGS)
def test_property_1_task_creation_completeness(text, project_id, status, priority):
    """
    Feature: kanban-board, Property 1: Task Creation Completeness
    Validates: Requirements 1.2, 6.5
    
    For any valid task data, creating a task should result in a task object
    containing all provided fields plus generated fields, and querying that
    task by ID should return the same data.
    """
    # Setup fresh database for this example
    with _fresh_database() as (db_path, db_manager, test_projects):
        # Ensure project exists
        assume(project_id in test_projects)
        
        # Ensure text doesn't contain secrets (for this property test)
        is_secret, _ = contains_secret(text)
        assume(not is_secret)
        
        # Create task
        task = db_manager.add_task(
            text=text,
            project_id=project_id,
            status=status,
            priority=priority
        )
        
        # Verify all provided fields are present
        assert task["text"] == text
        assert task["project_id"] == project_id
        assert task["status"] == status
        assert task["priority"] == priority
        
        # Verify generated fields are present
        assert "id" in task
        assert isinstance(task["id"], int)
        assert "created_at" in task
        assert "updated_at" in task
        assert isinstance(task["created_at"], str)
        assert isinstance(task["updated_at"], str)
        
        # Verify timestamps are valid ISO format
        datetime.fromisoformat(task["created_at"])
        datetime.fromisoformat(task["updated_at"])
        
        # Verify created_at and updated_at are initially equal
        assert task["created_at"] == task["updated_at"]
        
        # Verify persistence: query task by ID
        retrieved = db_manager.get_task(task["id"])
        assert retrieved is not None
        
        # Verify retrieved task matches created task
        assert retrieved["id"] == task["id"]
        assert retrieved["text"] == task["text"]
        assert retrieved["project_id"] == task["project_id"]
        assert retrieved["status"] == task["status"]
        assert retrieved["priority"] == task["priority"]
        assert retrieved["created_at"] == task["created_at"]
        assert retrieved["updated_at"] == task["updated_at"]


# Property 2: Query Filtering Accuracy
# For any set of tasks with various projects, statuses, and priorities,
# querying with filter parameters should return only tasks matching all specified filters
@given(
    num_tasks=st.integers(min_value=5, max_value=20),
    filter_project_id=st.one_of(st.none(), st.sampled_from(["project-tracker", "image-workflow", "trading-desk", "test-project-1", "test-project-2"])),
    filter_status=st.one_of(st.none(), st.sampled_from(["Backlog", "To Do", "In Progress", "Done"]))
)
@settings(max_examples=50, **DB_PROPERTY_SETTINGS)
def test_property_2_query_filtering_accuracy(num_tasks, filter_project_id, filter_status):
    """
    Feature: kanban-board, Property 2: Query Filtering Accuracy
    Validates: Requirements 1.4
    
    For any set of tasks with various projects, statuses, and priorities,
    querying with filter parameters should return only tasks matching all
    specified filters.
    """
    # Setup fresh database for this example
    with _fresh_database() as (db_path, db_manager, test_projects):
        # Create a diverse set of tasks
        all_projects = ["project-tracker", "image-workflow", "trading-desk", "test-project-1", "test-project-2"]
        all_statuses = ["Backlog", "To Do", "In Progress", "Done"]
        all_priorities = [None, "Critical", "High", "Medium", "Low"]
        
        created_tasks = []
        
        for i in range(num_tasks):
            project_id = all_projects[i % len(all_projects)]
            status = all_statuses[i % len(all_statuses)]
            priority = all_priorities[i % len(all_priorities)]
            text = f"Test task {i}"
            
            task = db_manager.add_task(
                text=text,
                project_id=project_id,
                status=status,
                priority=priority
            )
            created_tasks.append(task)
        
        # Query with filters
        results = db_manager.get_tasks(
            project_id=filter_project_id,
            status=filter_status
        )
        
        # Verify all results match the filters
        for task in results:
            if filter_project_id is not None:
                assert task["project_id"] == filter_project_id, \
                    f"Task {task['id']} has project_id {task['project_id']}, expected {filter_project_id}"
            
            if filter_status is not None:
                assert task["status"] == filter_status, \
                    f"Task {task['id']} has status {task['status']}, expected {filter_status}"
        
        # Verify we got all matching tasks (completeness check)
        expected_count = sum(
            1 for task in created_tasks
            if (filter_project_id is None or task["project_id"] == filter_project_id)
            and (filter_status is None or task["status"] == filter_status)
        )
        
        assert len(results) == expected_count, \
            f"Expected {expected_count} tasks matching filters, got {len(results)}"


def test_add_task_category_is_only_what_was_typed(tmp_path):
    """A card's category is the one given, stripped, or none: a project's `pt info` entries never fill it."""
    db_path = tmp_path / "test.db"
    create_database(db_path)
    db = BackendDatabaseManager(db_path)
    db.add_project(project_id="invoices", name="Invoices", path="/tmp/invoices", status="active")
    db.set_info("portfolio_label", "[AP]", project_id="invoices")

    def category(**kwargs):
        return db.add_task(text="t", project_id="invoices", status="Backlog", **kwargs)["category"]

    assert (category(), category(category=" [Ops] "), category(category="  ")) == (None, "[Ops]", None)


def test_task_text_is_stored_as_typed(tmp_path):
    # Escaping on store (#8246) showed "Owner&#x27;s" in pt and the dashboard,
    # and every re-save escaped again. HTML renderers escape at render time.
    db_path = tmp_path / "test.db"
    create_database(db_path)
    db = BackendDatabaseManager(db_path)
    db.add_project(project_id="p", name="P", path="/tmp/p", status="active")
    typed = "Owner's <b>Q&A</b> \"notes\""

    task = db.add_task(text=typed, project_id="p", status="Backlog")
    assert db.get_task(task["id"])["text"] == typed
    db.update_task(task["id"], text=typed + " again")
    assert db.get_task(task["id"])["text"] == typed + " again"


# Property 12: Secret Pattern Detection
# For any string containing secret patterns (API keys, SSNs, passwords),
# attempting to create a task with that text should either warn the user
# or block creation depending on confidence level
@given(
    secret_type=st.sampled_from(["openai_key", "api_key", "api_key_dash", "ssn", "long_token", "password_eq", "password_space"]),
    random_part=st.text(min_size=32, max_size=50, alphabet=st.sampled_from("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")),
    project_id=st.sampled_from(["project-tracker", "image-workflow", "trading-desk", "test-project-1", "test-project-2"]),
    status=st.sampled_from(["Backlog", "To Do", "In Progress", "Done"]),
)
@settings(max_examples=100, **DB_PROPERTY_SETTINGS)
def test_property_12_secret_pattern_detection(secret_type, random_part, project_id, status):
    """
    Feature: kanban-board, Property 12: Secret Pattern Detection
    Validates: Requirements 6.4, 14.1, 14.2
    
    For any string containing secret patterns, attempting to create a task
    with that text should either warn the user or block creation depending
    on confidence level.
    """
    # Setup fresh database for this example
    with _fresh_database() as (db_path, db_manager, test_projects):
        # Ensure project exists
        assume(project_id in test_projects)
        
        # Generate secret text based on type
        if secret_type == "openai_key":
            # Ensure at least 32 characters after "sk-" to match regex pattern
            secret_text = f"sk-{random_part[:32]}"
        elif secret_type == "api_key":
            secret_text = f"api_key={random_part}"
        elif secret_type == "api_key_dash":
            secret_text = f"api-key={random_part}"
        elif secret_type == "ssn":
            # Generate SSN format: XXX-XX-XXXX
            digits = "".join([c for c in random_part if c.isdigit()])
            if len(digits) >= 9:
                secret_text = f"{digits[:3]}-{digits[3:5]}-{digits[5:9]}"
            else:
                secret_text = f"123-45-6789"  # Fallback
        elif secret_type == "long_token":
            # Generate uppercase alphanumeric token
            token = "".join([c.upper() for c in random_part if c.isalnum()])[:20]
            if len(token) < 20:
                token = token + "A" * (20 - len(token))
            secret_text = token
        elif secret_type == "password_eq":
            secret_text = f"password={random_part}"
        else:  # password_space
            secret_text = f"password = {random_part}"
        
        # Verify secret detection function identifies the secret
        is_secret, pattern = contains_secret(secret_text)
        assert is_secret, f"Secret detection failed for text: {secret_text[:50]}..."
        
        # Attempt to create task with secret text
        # The system should reject this (raise ValueError or similar)
        with pytest.raises((ValueError, Exception)) as exc_info:
            db_manager.add_task(
                text=secret_text,
                project_id=project_id,
                status=status
            )
        
        # Verify error message mentions secret or validation
        error_msg = str(exc_info.value).lower()
        assert any(keyword in error_msg for keyword in ["secret", "pattern", "validation", "invalid", "blocked"]), \
            f"Error message should mention secret/validation, got: {error_msg}"


# Property 16: Project Validation
# For any task creation attempt with a non-existent project ID,
# the system should reject the creation and return an error
@given(
    invalid_project_id=st.text(
        min_size=1,
        max_size=50,
        alphabet=st.characters(min_codepoint=97, max_codepoint=122)  # lowercase letters
    ).filter(lambda s: s not in ["project-tracker", "image-workflow", "trading-desk", "test-project-1", "test-project-2"]),
    text=st.text(min_size=1, max_size=100).map(lambda s: s.strip()).filter(lambda s: len(s) > 0),
    status=st.sampled_from(["Backlog", "To Do", "In Progress", "Done"]),
    priority=st.one_of(st.none(), st.sampled_from(["Critical", "High", "Medium", "Low"]))
)
@settings(max_examples=50, **DB_PROPERTY_SETTINGS)
def test_property_16_project_validation(invalid_project_id, text, status, priority):
    """
    Feature: kanban-board, Property 16: Project Validation
    Validates: Requirements 14.3
    
    For any task creation attempt with a non-existent project ID,
    the system should reject the creation and return an error.
    """
    # Setup fresh database for this example
    with _fresh_database() as (db_path, db_manager, test_projects):
        # Ensure text doesn't contain secrets (we're testing project validation, not secret detection)
        is_secret, _ = contains_secret(text)
        assume(not is_secret)
        
        # Ensure project doesn't exist
        assume(invalid_project_id not in test_projects)
        
        # Verify project doesn't exist in database
        project = db_manager.get_project(invalid_project_id)
        assert project is None, f"Project {invalid_project_id} should not exist"
        
        # Attempt to create task with invalid project ID
        # The system should reject this (raise ValueError or similar)
        with pytest.raises((ValueError, Exception)) as exc_info:
            db_manager.add_task(
                text=text,
                project_id=invalid_project_id,
                status=status,
                priority=priority
            )
        
        # Verify error message mentions project or validation
        error_msg = str(exc_info.value).lower()
        assert any(keyword in error_msg for keyword in ["project", "not found", "invalid", "validation", "exist"]), \
            f"Error message should mention project/validation, got: {error_msg}"


def test_created_by_roundtrip():
    """created_by is stored on add_task and returned by get_task."""
    with _fresh_database() as (db_path, db_manager, _):
        task = db_manager.add_task(
            text="Origin-tagged task",
            project_id="project-tracker",
            status="Backlog",
            created_by="ai-metrics",
        )
        assert task["created_by"] == "ai-metrics"

        retrieved = db_manager.get_task(task["id"])
        assert retrieved is not None
        assert retrieved["created_by"] == "ai-metrics"


def test_created_by_defaults_to_null_when_absent():
    """created_by is optional; omitting it stores NULL, not an empty string or the literal 'None'."""
    with _fresh_database() as (db_path, db_manager, _):
        task = db_manager.add_task(
            text="No origin",
            project_id="project-tracker",
            status="Backlog",
        )
        assert task.get("created_by") is None

        retrieved = db_manager.get_task(task["id"])
        assert retrieved is not None
        assert retrieved.get("created_by") is None


def test_resolve_created_by_cwd_mapping(monkeypatch, tmp_path):
    """resolve_created_by() maps cwd and PT_CALLER_CWD to the expected labels."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
    from origin import resolve_created_by

    fake_home = tmp_path / "home"
    fake_projects = fake_home / "projects"
    (fake_projects / "ai-metrics" / "subdir").mkdir(parents=True)
    (fake_projects / "project-tracker").mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()

    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("PT_CALLER_CWD", raising=False)

    monkeypatch.chdir(fake_projects)
    assert resolve_created_by() == "architect"

    monkeypatch.chdir(fake_projects / "project-tracker")
    assert resolve_created_by() == "project-tracker"

    monkeypatch.chdir(fake_projects / "ai-metrics" / "subdir")
    assert resolve_created_by() == "ai-metrics"

    monkeypatch.chdir(outside)
    assert resolve_created_by() == "unknown"

    # PT_CALLER_CWD overrides cwd
    monkeypatch.chdir(outside)
    monkeypatch.setenv("PT_CALLER_CWD", str(fake_projects / "ai-metrics"))
    assert resolve_created_by() == "ai-metrics"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
