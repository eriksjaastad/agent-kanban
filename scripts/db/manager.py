"""Project Tracker database operations, executed locally."""
from .checkouts import CheckoutsMixin
from .jobs import JobsMixin
from .operations import ProjectTrackerOps
from .outreach import OutreachMixin

class DatabaseManager(
    JobsMixin, OutreachMixin, CheckoutsMixin, ProjectTrackerOps
):
    """The shared CLI/dashboard interface for the local database: one class.

    The SQL core (backend_manager), cross-table operations and recovery
    snapshots (operations), and the jobs, outreach and project checkout
    domains (mixins) are all methods of this object. Add-ons extend it in
    their own folders.
    """

__all__ = ["DatabaseManager"]
