import { afterEach, describe, expect, it, vi } from 'vitest';

import { NotesConflictError, toggleChecklistItem, updateTask } from './api';

afterEach(() => vi.unstubAllGlobals());

describe('toggleChecklistItem (#7821)', () => {
  it('POSTs the line, text, state and base notes to the string id, without retrying', async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve({ ok: true, json: () => Promise.resolve({ id: '98969975881101312' }) } as Response)
    );
    vi.stubGlobal('fetch', fetchMock);

    await toggleChecklistItem('98969975881101312', 3, 'third', true, 'a\n- [ ] third');

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe('/api/tasks/98969975881101312/checklist');
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body as string)).toEqual({
      line_index: 3,
      expected_text: 'third',
      checked: true,
      base_notes: 'a\n- [ ] third',
    });
  });

  it('maps 409 to NotesConflictError and does not retry a 500', async () => {
    const conflict = vi.fn(() =>
      Promise.resolve({ ok: false, status: 409, statusText: 'Conflict', json: () => Promise.resolve({ detail: 'moved' }) } as Response)
    );
    vi.stubGlobal('fetch', conflict);
    await expect(toggleChecklistItem('1', 0, 'x', true, null)).rejects.toBeInstanceOf(NotesConflictError);

    const boom = vi.fn(() =>
      Promise.resolve({ ok: false, status: 500, statusText: 'Err', json: () => Promise.resolve({ detail: 'boom' }) } as Response)
    );
    vi.stubGlobal('fetch', boom);
    await expect(toggleChecklistItem('1', 0, 'x', true, null)).rejects.toThrow('boom');
    expect(boom).toHaveBeenCalledTimes(1);
  });
});

describe('updateTask', () => {
  it('maps 409 to NotesConflictError and does not retry a 500', async () => {
    const conflict = vi.fn(() =>
      Promise.resolve({ ok: false, status: 409, statusText: 'Conflict', json: () => Promise.resolve({ detail: 'changed' }) } as Response)
    );
    vi.stubGlobal('fetch', conflict);
    await expect(updateTask('1', { notes: 'mine', base_notes: 'old' })).rejects.toBeInstanceOf(NotesConflictError);

    const boom = vi.fn(() =>
      Promise.resolve({ ok: false, status: 500, statusText: 'Err', json: () => Promise.resolve({ detail: 'boom' }) } as Response)
    );
    vi.stubGlobal('fetch', boom);
    const failure = updateTask('1', { status: 'Done' });
    await expect(failure).rejects.toThrow('boom');
    await expect(failure).rejects.not.toBeInstanceOf(NotesConflictError);
    expect(boom).toHaveBeenCalledTimes(1);
  });
});
