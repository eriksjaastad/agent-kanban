"""Validation utilities for Kanban board task management.

This module provides validation functions for task creation and updates,
including secret detection, text length validation, project existence checks,
enum validation for status and priority fields.
"""

import re
from typing import Optional, Tuple, Any


# Secret detection patterns from design.md
SECRET_PATTERNS = [
    r'sk-[a-zA-Z0-9]{32,}',                    # OpenAI keys
    r'api[_-]?key[_-]?=\s*["\']?[\w-]+',      # Generic API keys
    r'\d{3}-\d{2}-\d{4}',                      # SSN format
    r'[A-Z0-9]{20,}',                          # Generic long tokens
    r'password\s*=\s*["\']?[\w-]+',            # Passwords
]


# Valid status values for tasks
VALID_STATUSES = [
    "Backlog",
    "To Do",
    "In Progress",
    "Review",
    "Done",
    "Cancelled",
]

# Valid priority values for tasks
VALID_PRIORITIES = ["Critical", "High", "Medium", "Low"]

# Valid task types
VALID_TASK_TYPES = ["manual", "agent", "proposal"]

# A concrete, checkable acceptance-criterion line: "- [ ] <text>" or
# "- [x] <text>", with real text after the checkbox. A heading alone
# (e.g. "## Acceptance Criteria") does not match.
ACCEPTANCE_CRITERION_RE = re.compile(r'^[ \t]*-[ \t]*\[[ xX]\][ \t]+\S', re.MULTILINE)

ACCEPTANCE_CRITERIA_ERROR = (
    "Acceptance criteria required: add at least one checklist line stating "
    "how completion will be verified, e.g.\n"
    "    - [ ] <how completion is verified>\n"
    "A heading alone (e.g. '## Acceptance Criteria') does not count. For a "
    "proposal, state the decision/approval criterion instead, e.g. "
    "'- [ ] Approve if X; reject if Y'."
)

def _get_excluded_card_project_ids() -> set[str]:
    """Return the active exclusion set for card creation."""
    return set()


class BlockedTaskProjectError(ValueError):
    """Raised when task/card creation is intentionally blocked for a project."""


def get_blocked_card_project_ids() -> list[str]:
    """Return sorted project IDs that are intentionally excluded from card creation on this machine."""
    return sorted(_get_excluded_card_project_ids())


def get_blocked_card_reason(project_id: str) -> Optional[str]:
    """Return the user-facing reason a project is blocked on this machine, if any."""
    if project_id in _get_excluded_card_project_ids():
        return f"Cards cannot be created for project '{project_id}'"

    return None


def is_card_creation_allowed(project_id: str) -> bool:
    """Return True when task/card creation is allowed for a project on this machine."""
    return get_blocked_card_reason(project_id) is None


def contains_secret(text: str) -> Tuple[bool, str]:
    """Check if text contains secret patterns.
    
    Args:
        text: The text to check for secret patterns.
        
    Returns:
        A tuple of (is_secret, pattern_matched). If a secret is detected,
        returns (True, pattern). Otherwise returns (False, "").
        
    Examples:
        >>> contains_secret("sk-" + "12345678901234567890123456789012")  # Example API key pattern
        (True, 'sk-[a-zA-Z0-9]{32,}')
        >>> contains_secret("This is a normal task")
        (False, '')
    """
    for pattern in SECRET_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return (True, pattern)
    return (False, "")


def validate_task_text(text: str) -> Tuple[bool, Optional[str]]:
    """Validate task text length.
    
    Args:
        text: The task text to validate.
        
    Returns:
        A tuple of (is_valid, error_message). If valid, returns (True, None).
        If invalid, returns (False, error_message).
        
    Examples:
        >>> validate_task_text("Fix bug")
        (True, None)
        >>> validate_task_text("")
        (False, 'Task text must be between 1 and 1000 characters')
        >>> validate_task_text("x" * 1001)
        (False, 'Task text must be between 1 and 1000 characters')
    """
    if not text:
        return (False, "Task text must be between 1 and 1000 characters")
    
    if len(text) < 1:
        return (False, "Task text must be between 1 and 1000 characters")
    
    if len(text) > 1000:
        return (False, "Task text must be between 1 and 1000 characters")
    
    return (True, None)


def validate_project_exists(project_id: str, db_manager: Optional[Any] = None) -> Tuple[bool, Optional[str]]:
    """Validate that a project exists in the database.
    
    Args:
        project_id: The project ID to check.
        db_manager: Optional object with a get_project method. If not provided, creates a new DatabaseManager.
        
    Returns:
        A tuple of (exists, error_message). If project exists, returns (True, None).
        If project doesn't exist, returns (False, error_message).
        
    Examples:
        >>> from scripts.db.manager import DatabaseManager
        >>> db = DatabaseManager()
        >>> validate_project_exists("project-tracker", db)
        (True, None)
        >>> validate_project_exists("nonexistent-project", db)
        (False, "Project 'nonexistent-project' does not exist")
    """
    if not project_id:
        return (False, "Project ID is required")
    
    if db_manager is None:
        # Local import to avoid circular dependency
        from scripts.db.manager import DatabaseManager
        db_manager = DatabaseManager()
    
    project = db_manager.get_project(project_id)
    if project is None:
        return (False, f"Project '{project_id}' does not exist")
    
    return (True, None)


def validate_card_creation_project(project_id: str) -> Tuple[bool, Optional[str]]:
    """Validate that a project is allowed to receive pt task-board cards."""
    blocked_reason = get_blocked_card_reason(project_id)
    if blocked_reason:
        return (False, blocked_reason)

    return (True, None)


def validate_status(status: str) -> Tuple[bool, Optional[str]]:
    """Validate task status enum value.
    
    Args:
        status: The status value to validate.
        
    Returns:
        A tuple of (is_valid, error_message). If valid, returns (True, None).
        If invalid, returns (False, error_message).
        
    Examples:
        >>> validate_status("Backlog")
        (True, None)
        >>> validate_status("Invalid Status")
        (False, "Status must be one of: Backlog, To Do, In Progress, Done")
    """
    if status not in VALID_STATUSES:
        valid_list = ", ".join(VALID_STATUSES)
        return (False, f"Status must be one of: {valid_list}")
    
    return (True, None)


def validate_priority(priority: Optional[str]) -> Tuple[bool, Optional[str]]:
    """Validate task priority enum value.
    
    Args:
        priority: The priority value to validate. Can be None.
        
    Returns:
        A tuple of (is_valid, error_message). If valid, returns (True, None).
        If invalid, returns (False, error_message).
        
    Examples:
        >>> validate_priority("High")
        (True, None)
        >>> validate_priority(None)
        (True, None)
        >>> validate_priority("Invalid Priority")
        (False, "Priority must be one of: Critical, High, Medium, Low, or None")
    """
    if priority is None:
        return (True, None)
    
    if priority not in VALID_PRIORITIES:
        valid_list = ", ".join(VALID_PRIORITIES) + ", or None"
        return (False, f"Priority must be one of: {valid_list}")
    
    return (True, None)


def validate_task_type(task_type: Optional[str]) -> Tuple[bool, Optional[str]]:
    """Validate task type.

    Args:
        task_type: Task type to validate (manual, agent, or proposal)

    Returns:
        A tuple of (is_valid, error_message). If valid, returns (True, None).
        If invalid, returns (False, error_message).

    Examples:
        >>> validate_task_type("manual")
        (True, None)
        >>> validate_task_type("agent")
        (True, None)
        >>> validate_task_type("proposal")
        (True, None)
        >>> validate_task_type("invalid")
        (False, 'Task type must be one of: manual, agent, proposal')
    """
    if task_type is None:
        return (True, None)

    if task_type not in VALID_TASK_TYPES:
        valid_list = ", ".join(VALID_TASK_TYPES)
        return (False, f"Task type must be one of: {valid_list}")

    return (True, None)


def validate_acceptance_criteria(notes: Optional[str]) -> Tuple[bool, Optional[str]]:
    """Require a concrete, checkable acceptance criterion for a NEW card.

    Every new Kanban card must state how its own completion will be
    verified, as a Markdown checklist line: ``- [ ] <text>`` (or
    ``- [x] <text>``) with real text after the checkbox. A heading such as
    "## Acceptance Criteria" with no items under it does not satisfy this.

    Applies uniformly to every task_type, including "proposal" — a proposal
    is not exempt, it just states a decision/approval criterion instead of a
    dev task (see ACCEPTANCE_CRITERIA_ERROR for the exact wording agents and
    the CLI/API surface to callers). Subtasks (a parent_id set) are not
    exempt either.

    Where this is (and isn't) enforced:
        - Called explicitly by every user/agent-facing card-creation surface
          before it calls DatabaseManager.add_task(): ``pt tasks create``
          (scripts/pt.py) and ``POST /api/tasks`` (dashboard/app.py). Those two
          are the complete set of production creation routes (#7608).
        - NOT called from ``add_task()``/``validate_task_input()`` directly.
          ``add_task()`` is also the low-level DB primitive used by ~100
          internal test fixtures and maintenance scripts that aren't "a new
          card" in the product sense; enforcing there would have required
          rewriting all of them. New creation routes must remember to call
          this function themselves — see the two call sites above for the
          pattern.
        - NOT called from ``update_task()``: editing an existing card never
          requires retroactively adding criteria, so pre-#7608 cards stay
          readable and editable with no bulk migration.
        - NOT applied to the separate ``ideas`` table (``add_idea()``): those
          are pre-project, free-text thoughts with no status/workflow, not a
          Kanban card (see schema.py's "Ideas table for pre-project thoughts"
          comment) — out of scope for "every new Kanban card".

    Args:
        notes: The candidate notes/description field for a new card.

    Returns:
        A tuple of (is_valid, error_message).

    Examples:
        >>> validate_acceptance_criteria("- [ ] Run pytest and confirm green")
        (True, None)
        >>> validate_acceptance_criteria("## Acceptance Criteria")[0]
        False
        >>> validate_acceptance_criteria(None)[0]
        False
    """
    if not notes or not notes.strip():
        return (False, ACCEPTANCE_CRITERIA_ERROR)

    if not ACCEPTANCE_CRITERION_RE.search(notes):
        return (False, ACCEPTANCE_CRITERIA_ERROR)

    return (True, None)



def validate_task_input(
    text: str,
    project_id: str,
    status: str = "Backlog",
    priority: Optional[str] = None,
    task_type: Optional[str] = None,
    db_manager: Optional[Any] = None
) -> Tuple[bool, Optional[str]]:
    """Comprehensive validation for task creation/update.
    
    Validates all task fields in a single call:
    - Task text length (1-1000 characters)
    - Secret detection
    - Project existence
    - Status enum
    - Priority enum
    
    Args:
        text: Task text description.
        project_id: Project ID to associate task with.
        status: Task status (default: "Backlog").
        priority: Task priority (optional).
        db_manager: Optional object with a get_project method. If not provided, creates a new DatabaseManager.
        
    Returns:
        A tuple of (is_valid, error_message). If all validations pass,
        returns (True, None). If any validation fails, returns (False, error_message).
        
    Examples:
        >>> from scripts.db.manager import DatabaseManager
        >>> db = DatabaseManager()
        >>> validate_task_input("Fix bug", "project-tracker", "Backlog", "High", db)
        (True, None)
        >>> validate_task_input("", "project-tracker", "Backlog", None, db)
        (False, 'Task text must be between 1 and 1000 characters')
    """
    # Validate text length
    is_valid, error = validate_task_text(text)
    if not is_valid:
        return (False, error)
    
    # Check for secrets
    is_secret, pattern = contains_secret(text)
    if is_secret:
        return (False, f"Task text contains potential secret pattern: {pattern}")
    
    # Validate project exists
    is_valid, error = validate_project_exists(project_id, db_manager)
    if not is_valid:
        return (False, error)

    # Validate project is eligible for pt task-board cards
    is_valid, error = validate_card_creation_project(project_id)
    if not is_valid:
        return (False, error)
    
    # Validate status
    is_valid, error = validate_status(status)
    if not is_valid:
        return (False, error)
    
    # Validate priority
    is_valid, error = validate_priority(priority)
    if not is_valid:
        return (False, error)

    # Validate task type
    is_valid, error = validate_task_type(task_type)
    if not is_valid:
        return (False, error)

    return (True, None)
