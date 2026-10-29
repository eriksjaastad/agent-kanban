"""Parser for EXTERNAL_RESOURCES.yaml to extract service dependencies."""

import sys
import yaml
from pathlib import Path
from typing import Dict, List, Optional

# Add parent directory to path for config and logger imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from scripts.config import EXTERNAL_RESOURCES_FILE
from scripts.logger import get_logger

logger = get_logger(__name__)


def parse_external_resources(resources_path: Optional[Path] = None) -> Dict[str, List[Dict[str, any]]]:
    """
    Parse EXTERNAL_RESOURCES.yaml and extract services by project.
    
    Returns: {project_id: [{"service_name": str, "cost_monthly": float, "purpose": str}]}

    Raises when the file is missing, unreadable, invalid YAML, or has no
    'projects' mapping. The registry ships in the repo, so none of those is a
    first-run state, and `pt scan` syncs this result with deletes allowed: an
    empty dict would erase every project's services (#6900).
    """
    if resources_path is None:
        resources_path = EXTERNAL_RESOURCES_FILE
    
    # Try .yaml extension first, fall back to .md for backwards compatibility
    if not resources_path.exists():
        yaml_path = resources_path.parent / "EXTERNAL_RESOURCES.yaml"
        if yaml_path.exists():
            resources_path = yaml_path
        else:
            raise FileNotFoundError(
                f"External resources registry not found: neither {resources_path} "
                f"nor {yaml_path} exists (check PT_RESOURCES_FILE)"
            )
    
    # A read or YAML error raises: `pt scan` syncs this result with deletes
    # allowed, so an empty dict here would erase every project's services.
    with open(resources_path, 'r') as f:
        data = yaml.safe_load(f)
    
    if not isinstance(data, dict) or not isinstance(data.get('projects'), dict):
        raise ValueError(
            f"External resources registry {resources_path} has no 'projects' mapping"
        )
    
    services_by_project = {}
    
    for project_name, project_data in data['projects'].items():
        # project_name is already in the right format (e.g., "trading-projects")
        project_id = project_name
        
        if 'services' not in project_data:
            continue
        
        services = []
        for service in project_data['services']:
            # Skip local-only tools (SQLite, rclone, local services)
            service_type = service.get('type', '').lower()
            if service_type == 'local':
                continue
            
            # Skip database services (they're infrastructure, not external services)
            if service_type == 'database':
                continue
            
            service_name = service.get('name', '')
            if not service_name:
                continue
            
            services.append({
                "service_name": service_name,
                "cost_monthly": service.get('cost', 0),
                "purpose": service.get('purpose', '')
            })
        
        if services:
            services_by_project[project_id] = services
    
    return services_by_project



