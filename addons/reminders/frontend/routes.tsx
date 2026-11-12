import type { AddonRoute } from '@core/addons';
import { RemindersPage } from './RemindersPage';

export const routes: AddonRoute[] = [{ path: '/reminders', element: <RemindersPage /> }];
