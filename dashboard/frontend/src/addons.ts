import type { ReactElement } from 'react';

/** One page an add-on adds to the dashboard. */
export interface AddonRoute {
  path: string;
  element: ReactElement;
}

// Each addons/<name>/frontend/routes.tsx exports `routes`. They are found when
// the frontend is built, so this file never names an add-on, and an add-on whose
// folder is gone is simply not in the build.
const modules = import.meta.glob<{ routes?: AddonRoute[] }>(
  '../../../addons/*/frontend/routes.tsx',
  { eager: true },
);

export const addonRoutes: AddonRoute[] = Object.entries(modules).flatMap(([file, module]) => {
  if (!Array.isArray(module.routes)) {
    throw new Error(`${file} must export a routes array`);
  }
  return module.routes;
});
