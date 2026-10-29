import { render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { AgentChatPage } from './AgentChatPage';

globalThis.fetch = vi.fn() as typeof fetch;

vi.mock('./PageShell', () => ({
  PageShell: ({ children, title }: { children: React.ReactNode; title: string }) => (
    <div>
      <h1>{title}</h1>
      {children}
    </div>
  ),
}));

const message = { id: 7, ts: '2026-10-01T12:00:00Z', sender: 'architect', recipient: null, body: 'ship it' };

describe('AgentChatPage', () => {
  beforeEach(() => {
    vi.resetAllMocks();
  });

  it('shows a loading line, then the messages', async () => {
    vi.mocked(fetch).mockResolvedValueOnce({ ok: true, json: async () => ({ messages: [message] }) } as Response);
    render(<AgentChatPage />);
    expect(screen.getByText('Loading messages…')).toBeInTheDocument();
    expect(await screen.findByText('ship it')).toBeInTheDocument();
    expect(screen.queryByText('Loading messages…')).not.toBeInTheDocument();
  });

  it('says so when the board is empty', async () => {
    vi.mocked(fetch).mockResolvedValueOnce({ ok: true, json: async () => ({ messages: [] }) } as Response);
    render(<AgentChatPage />);
    expect(await screen.findByText('No messages on the board.')).toBeInTheDocument();
  });

  it('names the server detail when the request fails', async () => {
    vi.mocked(fetch).mockResolvedValueOnce({ ok: false, status: 503, json: async () => ({ detail: 'Agent Chat not configured' }) } as Response);
    render(<AgentChatPage />);
    expect(await screen.findByRole('alert')).toHaveTextContent('Agent Chat not configured');
  });

  it('falls back to the HTTP status when there is no detail', async () => {
    vi.mocked(fetch).mockResolvedValueOnce({ ok: false, status: 500, json: async () => { throw new Error('no body'); } } as unknown as Response);
    render(<AgentChatPage />);
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('HTTP 500'));
  });

  it('aborts its in-flight request on unmount', () => {
    vi.mocked(fetch).mockReturnValue(new Promise(() => {}));
    const view = render(<AgentChatPage />);
    const init = vi.mocked(fetch).mock.calls[0][1] as RequestInit;
    expect(init.signal?.aborted).toBe(false);
    view.unmount();
    expect(init.signal?.aborted).toBe(true);
  });
});
