import { render, screen, act, fireEvent } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ProjectsProvider } from './ProjectsProvider';
import { useProjects } from './useProjects';

vi.mock('../api', () => ({ fetchProjects: vi.fn() }));
import { fetchProjects } from '../api';

function Probe({ label }: { label: string }) {
  const { projects, error } = useProjects();
  return <div>{label}:{projects === null ? 'none' : projects.map((p) => p.id).join(',')}:{error?.message ?? ''}</div>;
}

describe('ProjectsProvider', () => {
  beforeEach(() => vi.resetAllMocks());

  it('fetches once and hands every consumer the same list', async () => {
    vi.mocked(fetchProjects).mockResolvedValue([{ id: 'a' }, { id: 'b' }] as never);
    render(<ProjectsProvider><Probe label="one" /><Probe label="two" /></ProjectsProvider>);
    expect(await screen.findByText('one:a,b:')).toBeTruthy();
    expect(screen.getByText('two:a,b:')).toBeTruthy();
    expect(fetchProjects).toHaveBeenCalledTimes(1);
  });

  it('keeps the list when a later reload fails, and aborts its request on unmount', async () => {
    vi.mocked(fetchProjects).mockResolvedValue([{ id: 'a' }] as never);
    function Grab() { return <button onClick={useProjects().reload}>reload</button>; }
    const { unmount } = render(<ProjectsProvider><Probe label="p" /><Grab /></ProjectsProvider>);
    expect(await screen.findByText('p:a:')).toBeTruthy();

    let signal: AbortSignal | undefined;
    vi.mocked(fetchProjects).mockImplementation((s?: AbortSignal) => { signal = s; return Promise.reject(new Error('boom')); });
    await act(async () => { fireEvent.click(screen.getByText('reload')); });
    expect(screen.getByText('p:a:boom')).toBeTruthy();

    vi.mocked(fetchProjects).mockImplementation((s?: AbortSignal) => { signal = s; return new Promise(() => {}); });
    await act(async () => { fireEvent.click(screen.getByText('reload')); });
    unmount();
    expect(signal?.aborted).toBe(true);
  });

  it('refetches when a consumer mounts later, so a failed load recovers', async () => {
    vi.mocked(fetchProjects).mockRejectedValueOnce(new Error('down'));
    const { rerender } = render(<ProjectsProvider><Probe label="one" /></ProjectsProvider>);
    expect(await screen.findByText('one:none:down')).toBeTruthy();
    expect(fetchProjects).toHaveBeenCalledTimes(1);

    vi.mocked(fetchProjects).mockResolvedValueOnce([{ id: 'a' }] as never);
    rerender(<ProjectsProvider><Probe label="one" /><Probe label="two" /></ProjectsProvider>);
    expect(await screen.findByText('two:a:')).toBeTruthy();
    expect(screen.getByText('one:a:')).toBeTruthy();
    expect(fetchProjects).toHaveBeenCalledTimes(2);
  });
});
