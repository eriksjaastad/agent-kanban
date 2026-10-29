"""Metadata providers for project discovery."""

from abc import ABC, abstractmethod
from typing import Optional, List, Dict, Any
from pathlib import Path

# Configure logging using project-specific logger
import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from scripts.logger import get_logger

logger = get_logger(__name__)

class MetadataProvider(ABC):
    """Abstract base class for project metadata providers."""
    
    @abstractmethod
    def get_health(self, project_path: str) -> Optional[Dict[str, Any]]:
        """Returns {"score": 0-100, "grade": "A-F"} or None if unavailable."""
        pass
    
    @abstractmethod
    def get_tasks(self, project_path: Optional[str] = None) -> List[Dict[str, Any]]:
        """Returns list of task dicts from parsing."""
        pass
    
    @abstractmethod
    def check_file(self, file_path: str) -> Dict[str, Any]:
        """Returns {"valid": bool, "issues": [...]}."""
        pass
    
    @abstractmethod
    def fix_file(self, file_path: str) -> bool:
        """Returns True if fixed successfully."""
        pass

class LegacyProvider(MetadataProvider):
    """Concrete provider that uses existing Python logic."""
    
    def get_health(self, project_path: str) -> Optional[Dict[str, Any]]:
        """Legacy Python logic doesn't compute health scores."""
        return None
    
    def get_tasks(self, project_path: Optional[str] = None) -> List[Dict[str, Any]]:
        """Uses existing todo_parser.py logic (Interface only)."""
        raise NotImplementedError("LegacyProvider.get_tasks not yet implemented")
    
    def check_file(self, file_path: str) -> Dict[str, Any]:
        """Uses existing validation logic (Interface only)."""
        raise NotImplementedError("LegacyProvider.check_file not yet implemented")
    
    def fix_file(self, file_path: str) -> bool:
        """Legacy logic doesn't support auto-fixing."""
        return False

def get_provider() -> MetadataProvider:
    """Return the metadata provider.

    Until 2026-09-15 this probed for the Go `audit` binary from the audit-agent
    project and returned an AuditProvider wrapping it. audit-agent was archived
    (see ~/projects/_archive/audit-agent/ARCHIVED.md); its functions live in pt
    itself now, so the binary no longer exists and that branch was unreachable.

    Removing it also stops a pointless subprocess: with the binary gone,
    `shutil.which("audit")` resolved to macOS's own /usr/sbin/audit — the BSD
    audit daemon utility — and every call spawned it just to reject it on a
    help-text match.
    """
    return LegacyProvider()
