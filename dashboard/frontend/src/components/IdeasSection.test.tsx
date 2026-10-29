// Snowflake-scale idea ids must reach the API call as the exact string (#7826).
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { IdeasSection } from './IdeasSection';

const BIG_ID = '98969975881101312';
const NEXT_ID = '98969975881101313';

afterEach(() => {
  vi.unstubAllGlobals();
});

function stubApi() {
  // Raw JSON text parsed by the real JSON.parse, as Response.json() does.
  const listText = `{"ideas":[{"id":"${BIG_ID}","text":"first idea","created_at":"2026-10-01T00:00:00","updated_at":"2026-10-01T00:00:00"},{"id":"${NEXT_ID}","text":"second idea","created_at":"2026-10-01T00:00:00","updated_at":"2026-10-01T00:00:00"}]}`;
  const fetchMock = vi.fn((_url: string, options?: RequestInit) => {
    const body = options?.method ? '{}' : listText;
    return Promise.resolve({
      ok: true,
      status: 200,
      json: () => Promise.resolve(JSON.parse(body)),
    } as Response);
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

function writeCalls(fetchMock: ReturnType<typeof stubApi>, method: string) {
  return fetchMock.mock.calls.filter(([, o]) => (o as RequestInit | undefined)?.method === method);
}

describe('IdeasSection id precision', () => {
  it('edit PATCHes the exact string id', async () => {
    const user = userEvent.setup();
    const fetchMock = stubApi();
    render(<IdeasSection />);

    await screen.findByText('first idea');
    const card = screen.getByText('first idea').closest('.idea-card') as HTMLElement;
    // Hover-revealed actions: user.click's pointer move fires mouseleave on the
    // card before the click lands, so click these buttons directly.
    fireEvent.mouseEnter(card);
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
    const textarea = screen.getByPlaceholderText('Enter your idea...');
    await user.clear(textarea);
    await user.type(textarea, 'changed');
    await user.click(screen.getByRole('button', { name: 'Update' }));

    await waitFor(() => expect(writeCalls(fetchMock, 'PATCH')).toHaveLength(1));
    expect(writeCalls(fetchMock, 'PATCH')[0][0]).toBe(`/api/ideas/${BIG_ID}`);
  });

  it('delete sends the exact string id', async () => {
    const fetchMock = stubApi();
    render(<IdeasSection />);

    await screen.findByText('second idea');
    const card = screen.getByText('second idea').closest('.idea-card') as HTMLElement;
    fireEvent.mouseEnter(card);
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }));

    await waitFor(() => expect(writeCalls(fetchMock, 'DELETE')).toHaveLength(1));
    expect(writeCalls(fetchMock, 'DELETE')[0][0]).toBe(`/api/ideas/${NEXT_ID}`);
  });
});
