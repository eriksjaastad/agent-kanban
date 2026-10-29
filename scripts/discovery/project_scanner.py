"""Project scanner for auto-discovery."""

import sys
import re
from pathlib import Path
from typing import List, Dict, Any, Optional, Union
from concurrent.futures import ThreadPoolExecutor, as_completed

from .git_metadata import get_last_modified
from .providers import get_provider

# Add parent directory to path for config and logger imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from scripts.config import PROJECTS_BASE_DIR
from scripts.logger import get_logger

logger = get_logger(__name__)

def clean_description(text: Optional[str]) -> str:
    """Remove placeholders and malformed wiki-style links from descriptions."""
    if not text:
        return ""
    cleaned = re.sub(
        r"<!--\s*SCAFFOLD:START.*?SCAFFOLD:END.*?-->",
        "",
        text,
        flags=re.DOTALL | re.IGNORECASE
    )
    cleaned = re.sub(r"<!--.*?-->", "", cleaned, flags=re.DOTALL)
    cleaned = re.sub(r"\{\{[^}]+\}\}", "", cleaned)
    cleaned = re.sub(r"\[([^\]|]+)\|([^\]]+)\]\([^)]+\)", r"\2", cleaned)
    cleaned = " ".join(cleaned.split())
    return cleaned.strip()


def discover_projects(
    base_path: Optional[Union[str, Path]] = None,
) -> List[Dict[str, Any]]:
    """Scan directory for projects."""
    if base_path is None:
        base_path = PROJECTS_BASE_DIR
    if isinstance(base_path, str) and not base_path.strip():
        logger.warning("PROJECTS_BASE_DIR is empty; cannot scan for projects.")
        return []
    base = Path(base_path)
    
    if not base.exists():
        logger.warning(f"Projects base path does not exist: {base}")
        return []
    
    if not base.is_dir():
        logger.warning(f"Projects base path is not a directory: {base}")
        return []
    
    projects = []
    
    # Check if directory is empty (no subdirectories)
    base_items = list(base.iterdir())
    subdirs = [item for item in base_items if item.is_dir()]
    if not subdirs:
        logger.info(f"Projects base path is empty (no subdirectories found): {base}")
        return []

    marker_files = [
        ".git",
        "README.md",
        "pyproject.toml",
        "requirements.txt",
        "Pipfile",
        "package.json",
        "tsconfig.json",
        "Cargo.toml",
        "go.mod",
        "Gemfile",
        "composer.json",
        "Makefile"
    ]
    code_dirs = ["src", "lib", "app", "apps", "packages", "backend", "frontend", "server", "client"]
    code_exts = [".py", ".js", ".ts"]
    
    for item in base_items:
        if not item.is_dir() or should_skip_directory(item):
            continue

        # Check for indicators of a project (fast path)
        has_marker = (item / ".git").is_dir() or any((item / marker).exists() for marker in marker_files)

        # If no markers, do limited code file checks in common code dirs
        has_code = False
        if not has_marker:
            for code_dir in code_dirs:
                candidate = item / code_dir
                if not candidate.is_dir():
                    continue
                for ext in code_exts:
                    # Fast shallow check - only look at top level, not recursive
                    if any(candidate.glob(f"*{ext}")):
                        has_code = True
                        break
                if has_code:
                    break

        # If it looks like a project, extract metadata
        if has_marker or has_code:
            project = extract_project_metadata(item)
            if project:
                projects.append(project)
    
    return projects


def scan_health_parallel(projects: List[Dict], max_workers: int = 8) -> Dict[str, Dict]:
    """Run health checks in parallel, return {project_id: {"score": N, "grade": "X"}}."""
    provider = get_provider()
    results = {}
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(provider.get_health, p["path"]): p["id"]
            for p in projects
        }
        for future in as_completed(futures):
            project_id = futures[future]
            try:
                results[project_id] = future.result()
            except Exception as e:
                logger.error(f"Health check failed for {project_id}: {e}")
                results[project_id] = None
    
    return results



def extract_project_metadata(project_path: Path) -> Dict[str, Any]:
    """Extract all metadata from a project."""
    metadata = {
        "id": project_path.name.lower().replace(" ", "-"),
        "name": project_path.name,
        "path": str(project_path),
        "last_modified": get_last_modified(project_path),
        "status": "unknown",
        "phase": None,
        "description": None,
        "completion_pct": 0,
        "ai_agents": [],
        "cron_jobs": [],
        "services": [],
        "is_infrastructure": False,
        "has_index": False,
        "index_is_valid": False,
        "index_updated_at": None,
        "project_type": "standard",
    }
    
    # Index metadata fields kept for schema compat but always default
    # (00_Index files removed — Librarian system deleted)

    # Parse README.md for description
    if not metadata["description"]:
        readme_path = project_path / "README.md"
        if readme_path.exists():
            metadata["description"] = clean_description(extract_readme_description(readme_path))


    return metadata


def extract_readme_description(readme_path: Path) -> str:
    """Extract first paragraph from README."""
    try:
        content = readme_path.read_text()
        lines = content.split('\n')
        
        # Skip title and empty lines
        description_lines = []
        skip_initial = True
        
        for line in lines:
            # Skip title
            if line.startswith('#'):
                skip_initial = False
                continue
            
            # Skip metadata/badges
            if line.startswith('[![') or line.startswith('[!'):
                continue
            
            # Skip empty lines at start
            if skip_initial and not line.strip():
                continue
            
            skip_initial = False
            
            # Stop at next heading
            if line.startswith('#'):
                break
            
            # Stop at horizontal rule
            if line.startswith('---'):
                break
            
            # Collect non-empty lines
            if line.strip():
                description_lines.append(line.strip())
            elif description_lines:
                # Stop at first blank line after content
                break
        
        description = ' '.join(description_lines)
        
        # Limit length
        if len(description) > 200:
            description = description[:197] + "..."
        
        return description
    except (OSError, UnicodeDecodeError) as e:  # governance: allow-silent SF002: an unreadable README only means no description, a cosmetic field; other errors raise
        logger.warning(f"Failed to extract description from {readme_path}: {e}")
        return ""


def _load_ptignore() -> set:
    """Load directory names to skip from .ptignore file."""
    ptignore_path = Path(PROJECTS_BASE_DIR) / ".ptignore"
    ignore_names = set()

    if ptignore_path.exists():
        try:
            for line in ptignore_path.read_text().splitlines():
                line = line.strip()
                # Skip empty lines and comments
                if line and not line.startswith('#'):
                    ignore_names.add(line)
        except Exception as e:
            logger.warning(f"Failed to read .ptignore: {e}")

    return ignore_names


# Cache the ptignore contents (loaded once per process)
_ptignore_cache = None


def should_skip_directory(dir_path: Path) -> bool:
    """Determine if a directory should be skipped."""
    global _ptignore_cache

    # Load .ptignore on first call
    if _ptignore_cache is None:
        _ptignore_cache = _load_ptignore()

    # Built-in skip list (common non-project directories)
    skip_names = {
        "node_modules",
        ".git",
        "__pycache__",
        "venv",
        "env",
        ".venv",
        "dist",
        "build",
        ".DS_Store",
        ".idea",
        ".vscode",
        "_trash",
        "trash",
        "archives",
        "_archive",
        "logs",
        "data",
        "plugin-duplicate-detection",
        "plugin-find-names-chrome"
    }

    # Combine built-in skip list with .ptignore
    all_skip_names = skip_names | _ptignore_cache

    return dir_path.name in all_skip_names or dir_path.name.startswith('.')
