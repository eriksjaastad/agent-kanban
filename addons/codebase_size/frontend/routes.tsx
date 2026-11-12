import type { AddonRoute } from '@core/addons';
import { CodebasePage } from './CodebasePage';

export const routes: AddonRoute[] = [{ path: '/codebase', element: <CodebasePage /> }];
