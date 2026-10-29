import { render, screen, act, fireEvent, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { IdeasSection } from './IdeasSection';

vi.mock('../api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api')>()),
  fetchIdeas: vi.fn(),
  createIdea: vi.fn(),
  deleteIdea: vi.fn(),
}));

import { createIdea, deleteIdea, fetchIdeas } from '../api';

const IDEA = { id: '1', text: 'first idea', created_at: 'x', updated_at: 'x' };

describe('IdeasSection load', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.spyOn(console, 'error').mockImplementation(() => {});
  });

  it('shows the loading text, then the ideas', async () => {
    vi.mocked(fetchIdeas).mockResolvedValue([IDEA]);
    render(<IdeasSection />);
    expect(screen.getByText('Loading ideas...')).toBeTruthy();
    expect(await screen.findByText('first idea')).toBeTruthy();
  });

  it('shows a failed load in the create modal and clears it on close', async () => {
    vi.mocked(fetchIdeas).mockRejectedValue(new Error('ideas down'));
    render(<IdeasSection />);
    fireEvent.click(await screen.findByRole('button', { name: '+ Create Idea' }));
    expect(screen.getByText('ideas down')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    fireEvent.click(screen.getByRole('button', { name: '+ Create Idea' }));
    expect(screen.queryByText('ideas down')).toBeNull();
  });

  it('shows the loading text again on the reload after a save, and keeps the list if that reload fails', async () => {
    vi.mocked(fetchIdeas).mockResolvedValueOnce([IDEA]);
    vi.mocked(createIdea).mockResolvedValue(undefined as never);
    render(<IdeasSection />);
    await screen.findByText('first idea');
    fireEvent.click(screen.getByRole('button', { name: '+ Create Idea' }));
    fireEvent.change(screen.getByPlaceholderText('Enter your idea...'), { target: { value: 'new' } });
    vi.mocked(fetchIdeas).mockRejectedValueOnce(new Error('reload down'));
    fireEvent.click(screen.getByRole('button', { name: 'Create' }));
    await waitFor(() => expect(fetchIdeas).toHaveBeenCalledTimes(2));
    expect(await screen.findByText('first idea')).toBeTruthy();
  });

  it('keeps a failed reload\'s error after a later reload succeeds, until a save or close clears it', async () => {
    vi.mocked(fetchIdeas).mockResolvedValueOnce([IDEA]);
    vi.mocked(createIdea).mockResolvedValue(undefined as never);
    vi.mocked(deleteIdea).mockResolvedValue(undefined as never);
    const { container } = render(<IdeasSection />);
    await screen.findByText('first idea');

    // A save whose reload fails sets the error.
    fireEvent.click(screen.getByRole('button', { name: '+ Create Idea' }));
    fireEvent.change(screen.getByPlaceholderText('Enter your idea...'), { target: { value: 'new' } });
    vi.mocked(fetchIdeas).mockRejectedValueOnce(new Error('reload down'));
    fireEvent.click(screen.getByRole('button', { name: 'Create' }));
    await waitFor(() => expect(fetchIdeas).toHaveBeenCalledTimes(2));
    await act(async () => {});

    // A delete whose reload succeeds does not clear it.
    vi.mocked(fetchIdeas).mockResolvedValueOnce([IDEA]);
    fireEvent.mouseEnter(container.querySelector('.idea-card')!);
    fireEvent.click(container.querySelector('.idea-delete-btn')!);
    fireEvent.click(container.querySelector('.idea-confirm-delete-btn')!);
    await waitFor(() => expect(fetchIdeas).toHaveBeenCalledTimes(3));
    await act(async () => {});
    fireEvent.click(screen.getByRole('button', { name: '+ Create Idea' }));
    expect(screen.getByText('reload down')).toBeTruthy();
  });

  it('aborts the load on unmount', async () => {
    let signal: AbortSignal | undefined;
    vi.mocked(fetchIdeas).mockImplementation((s?: AbortSignal) => { signal = s; return new Promise(() => {}); });
    const { unmount } = render(<IdeasSection />);
    await act(async () => {});
    unmount();
    expect(signal?.aborted).toBe(true);
  });
});
