import { useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import type { Project } from '../types';
import { useProjects } from '../hooks/useProjects';
import { CheckoutBadge } from './CheckoutBadge';
import './ProjectFilterModal.css';

interface ProjectFilterModalProps {
  isOpen: boolean;
  onClose: () => void;
  currentProject?: string;
}

type ProjectOption = Project & { task_count?: number };


export function ProjectFilterModal({
  isOpen,
  onClose,
  currentProject,
}: ProjectFilterModalProps) {
  const navigate = useNavigate();
  const { projects: loaded, error: loadError, loading: reloading, reload } = useProjects();
  const [searchTerm, setSearchTerm] = useState('');
  // Each open (a fresh mount in KanbanBoard) refetches. With no list yet it
  // shows "Loading projects..." until that settles; a last good list stays
  // visible meanwhile. A previous failure is not shown while the retry runs.
  const loading = loaded === null && (reloading || loadError === null);
  const error = loadError && !reloading ? loadError.message : null;

  useEffect(() => {
    if (!isOpen) {
      return;
    }

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        onClose();
      }
    };

    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [isOpen, onClose]);

  // Every open refreshes the list, since task counts go stale. Mounting open
  // is covered by useProjects()' own mount refetch (KanbanBoard mounts the
  // modal only when it opens), so only a later closed-to-open change reloads.
  const wasOpen = useRef(isOpen);
  useEffect(() => {
    if (isOpen && !wasOpen.current) reload();
    wasOpen.current = isOpen;
  }, [isOpen, reload]);

  const filteredProjects = useMemo(() => {
    const query = searchTerm.trim().toLowerCase();
    const list = [...((loaded ?? []) as ProjectOption[])].sort((a, b) =>
      a.name.localeCompare(b.name, undefined, { sensitivity: 'base' })
    );

    if (!query) {
      return list;
    }

    return list.filter((project) => {
      return (
        project.name.toLowerCase().includes(query) ||
        project.id.toLowerCase().includes(query)
      );
    });
  }, [loaded, searchTerm]);

  const handleSelect = (projectId?: string) => {
    if (!projectId) {
      navigate('/kanban');
    } else {
      navigate(`/kanban/${encodeURIComponent(projectId)}`);
    }
    onClose();
  };

  if (!isOpen) {
    return null;
  }

  return (
    <div className="project-filter-modal-overlay" onClick={onClose}>
      <div
        className="project-filter-modal"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="project-filter-modal-header">
          <h2>Filter by Project</h2>
          <button
            className="project-filter-modal-close"
            onClick={onClose}
            aria-label="Close modal"
          >
            ×
          </button>
        </div>

        <input
          className="project-filter-search"
          type="text"
          placeholder="Search projects..."
          value={searchTerm}
          onChange={(event) => setSearchTerm(event.target.value)}
          autoFocus
        />

        {loading && <div className="project-filter-loading">Loading projects...</div>}
        {error && <div className="project-filter-error">{error}</div>}

        {!loading && !error && (
          <div className="project-filter-list">
            <button
              className={`project-filter-item ${!currentProject ? 'active' : ''}`}
              onClick={() => handleSelect()}
            >
              <span className="project-filter-name">All Projects</span>
            </button>

            {filteredProjects.length === 0 ? (
              <div className="project-filter-empty">No projects found</div>
            ) : (
              <div className="project-filter-group">
                  {filteredProjects.map((project) => (
                    <button
                      key={project.id}
                      className={`project-filter-item ${
                        currentProject === project.id ? 'active' : ''
                      }`}
                      onClick={() => handleSelect(project.id)}
                    >
                      <span className="project-filter-name-row">
                        <span className="project-filter-name">{project.name}</span>
                        {project.checkout && <CheckoutBadge projectId={project.id} checkout={project.checkout} />}
                      </span>
                      {typeof project.task_count === 'number' && (
                        <span className="project-filter-count">{project.task_count}</span>
                      )}
                    </button>
                  ))}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
