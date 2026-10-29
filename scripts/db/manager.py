"""Project Tracker database operations, executed locally."""
from .checkouts import CheckoutsMixin
from .codebase_size import CodebaseSizeMixin
from .jobs import JobsMixin
from .operations import ProjectTrackerOps
from .outreach import OutreachMixin
from .reminders import RemindersMixin

class DatabaseManager(
    JobsMixin, OutreachMixin, RemindersMixin, CheckoutsMixin, CodebaseSizeMixin, ProjectTrackerOps
):
    """The shared CLI/dashboard interface for the local database: one class.

    The SQL core (backend_manager), cross-table operations and recovery
    snapshots (operations), and the jobs, outreach, reminders, project
    checkout and codebase-size domains (mixins) are all methods of this object.
    """

__all__ = ["DatabaseManager"]
