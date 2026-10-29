"""Calendar operations against the configured local database."""
from .backend_calendar_manager import CalendarManager as BackendCalendarManager
from .manager import DatabaseManager

class CalendarManager(BackendCalendarManager):
    def __init__(self, db_path=None):
        manager = DatabaseManager(db_path)
        super().__init__(manager.db_path)
