import { lazy, Suspense, useState } from 'react';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Link, MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { browser } from '../utils/browser';
import { PageBoundary } from './RouteErrorBoundary';

// What a tab opened before a redeploy gets: the old chunk is gone.
const MissingChunkPage = lazy(() =>
  Promise.reject(new TypeError('Failed to fetch dynamically imported module: /assets/JobsPage-old.js')));

function renderApp(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <nav>
        <Link to="/kanban">Kanban</Link>
      </nav>
      <PageBoundary>
        <Suspense fallback={<p>Loading…</p>}>
          <Routes>
            <Route path="/jobs" element={<MissingChunkPage />} />
            <Route path="/kanban" element={<p>Kanban board</p>} />
          </Routes>
        </Suspense>
      </PageBoundary>
    </MemoryRouter>,
  );
}

function Board() {
  const [clicks, setClicks] = useState(0);
  return <button onClick={() => setClicks(clicks + 1)}>clicks {clicks}</button>;
}

describe('PageBoundary', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('keeps the app and offers a reload when a page chunk fails to load', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    const reload = vi.spyOn(browser, 'reload').mockImplementation(() => {});
    const user = userEvent.setup();
    renderApp('/jobs');

    expect(await screen.findByRole('alert')).toHaveTextContent("This page couldn't load");
    expect(screen.getByRole('link', { name: 'Kanban' })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Reload' }));
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it('clears the failure when the user moves to another page', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    const user = userEvent.setup();
    renderApp('/jobs');

    await screen.findByRole('alert');
    await user.click(screen.getByRole('link', { name: 'Kanban' }));
    expect(screen.getByText('Kanban board')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('keeps a page and its state across a path change within the same page', async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={['/kanban']}>
        <Link to="/kanban/alpha">alpha</Link>
        <PageBoundary>
          <Routes>
            <Route path="/kanban" element={<Board />} />
            <Route path="/kanban/:project" element={<Board />} />
          </Routes>
        </PageBoundary>
      </MemoryRouter>,
    );
    await user.click(screen.getByRole('button', { name: 'clicks 0' }));
    await user.click(screen.getByRole('link', { name: 'alpha' }));
    expect(screen.getByRole('button')).toHaveTextContent('clicks 1');
  });
});
