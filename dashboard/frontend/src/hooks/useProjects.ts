import { createContext, useContext, useEffect } from 'react';
import type { Project } from '../types';

export interface ProjectsValue {
  /** The last good project list as the server sent it; null until one load succeeds. */
  projects: Project[] | null;
  /** Error from the latest load; kept alongside `projects` when a reload fails. */
  error: Error | null;
  loading: boolean;
  /** Fetches again; call from event handlers (a no-op before the provider's first run). */
  reload: () => void;
}

export const ProjectsContext = createContext<ProjectsValue | null>(null);

/**
 * The app-wide project list. Consumers keep any sorting or filtering local.
 *
 * Each consumer mount refetches, as each consumer fetched for itself before:
 * the list stays fresh across navigation and a failed load recovers on the
 * next mount. Consumers mounting together share the newest request. At app
 * start this runs before the provider's first load and does nothing.
 */
export function useProjects(): ProjectsValue {
  const value = useContext(ProjectsContext);
  const reload = value?.reload;
  useEffect(() => {
    reload?.();
  }, [reload]);
  if (!value) throw new Error('useProjects must be used inside <ProjectsProvider>');
  return value;
}
