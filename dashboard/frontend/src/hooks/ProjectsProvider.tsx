import { useEffect } from 'react';
import type { ReactNode } from 'react';
import { fetchProjects } from '../api';
import { ProjectsContext } from './useProjects';
import { useRequest } from './useRequest';

/** Loads the project list once for the whole app. */
export function ProjectsProvider({ children }: { children: ReactNode }) {
  const { data, error, loading, reload } = useRequest(fetchProjects, []);

  useEffect(() => {
    if (error) console.error('Failed to load projects:', error);
  }, [error]);

  return (
    <ProjectsContext.Provider value={{ projects: data, error, loading, reload }}>
      {children}
    </ProjectsContext.Provider>
  );
}
