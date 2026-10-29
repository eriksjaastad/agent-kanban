import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { OUTREACH_REQUEST_TIMEOUT_MS, OutreachPanel } from './OutreachPanel';

globalThis.fetch = vi.fn() as typeof fetch;

interface Contact {
  id: number;
  name: string;
  contacted_at: string | null;
  created_at: string;
}

interface StoredContact extends Contact {
  replied_at?: string | null;
  deleted_at?: string | null;
}

function jsonResponse(payload: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
  } as Response;
}

type DeferredKind = 'patch' | 'contacted' | 'replied' | 'delete' | 'add' | 'get';

interface MockOutreachApiOptions {
  deferPatch?: boolean;
  deferContacted?: boolean;
  deferReplied?: boolean;
  deferDelete?: boolean;
  deferAdd?: boolean;
  /** Simulates another tab having contacted the row first: POST /contacted returns 409 and the row is already contacted on the server. */
  contactedConflict?: boolean | ((id: number) => boolean);
  /** Simulates a network failure: POST /contacted rejects instead of returning a response. */
  contactedNetworkError?: boolean | ((id: number) => boolean);
  /** Simulates a server hang: POST /contacted never settles on its own and rejects with an AbortError when the request is aborted. */
  hangContacted?: boolean | ((id: number) => boolean);
  /** Simulates a server hang: every GET never settles on its own and rejects with an AbortError when the request is aborted. */
  hangGet?: boolean;
  /** Simulates a server-side failure (HTTP 500) on a PATCH rename. Indexed by call order across all PATCH attempts (successful or not). */
  patchFailure?: boolean | ((callIndex: number) => boolean);
  /** Simulates a network failure: PATCH rejects instead of returning a response. Indexed like patchFailure. */
  patchNetworkError?: boolean | ((callIndex: number) => boolean);
  /** Simulates a server hang on PATCH: never settles on its own, rejects with an AbortError when aborted. Indexed like patchFailure. */
  hangPatch?: boolean | ((callIndex: number) => boolean);
  /** Simulates another client deleting the row between load and this PATCH: returns 404 AND marks the contact deleted server-side, so a follow-up GET genuinely omits it. Indexed like patchFailure. */
  patchDeletedElsewhere?: boolean | ((callIndex: number) => boolean);
  /** Simulates another client replying to the row between load and this PATCH: returns 409 AND marks the contact replied server-side, so a follow-up GET genuinely omits it. Indexed like patchFailure. */
  patchRepliedElsewhere?: boolean | ((callIndex: number) => boolean);
  /** Defers every GET after the first (the initial load resolves normally); release with resolve('get', index). Lets a test control exactly when a post-error resync lands. */
  deferGetAfterFirst?: boolean;
}

interface MockOutreachApi {
  patchCalls: Array<{ id: number; name: string }>;
  contactedCalls: number[];
  repliedCalls: number[];
  deleteCalls: number[];
  addCalls: number;
  /** Highest number of concurrently pending fetch calls the component ever produced. */
  readonly maxInFlight: number;
  resolvePatch: (index?: number) => void;
  resolve: (kind: 'contacted' | 'replied' | 'delete' | 'add' | 'get', index?: number) => void;
}

function mockOutreachApi(
  initial: Contact[] = [],
  options: MockOutreachApiOptions = {}
): MockOutreachApi {
  const contacts: StoredContact[] = initial.map((contact) => ({ ...contact }));
  let nextId = contacts.reduce((max, contact) => Math.max(max, contact.id), 0) + 1;
  const patchCalls: Array<{ id: number; name: string }> = [];
  const contactedCalls: number[] = [];
  const repliedCalls: number[] = [];
  const deleteCalls: number[] = [];
  let addCalls = 0;
  let getCallCount = 0;
  let inFlight = 0;
  let maxInFlight = 0;
  const deferred: Array<{ kind: DeferredKind; resolve: (response: Response) => void; response: Response }> = [];

  function track(response: Response | Promise<Response>): Promise<Response> {
    inFlight += 1;
    maxInFlight = Math.max(maxInFlight, inFlight);
    return Promise.resolve(response).finally(() => {
      inFlight -= 1;
    });
  }

  function defer(kind: DeferredKind, response: Response): Promise<Response> {
    return new Promise((resolve) => {
      deferred.push({ kind, resolve, response });
    });
  }

  // A request that never settles on its own (server hang, stalled connection).
  // It honours the caller's AbortSignal by rejecting with an AbortError when
  // aborted, exactly like a real fetch whose signal fired.
  function hang(signal: AbortSignal | null | undefined): Promise<Response> {
    return new Promise<Response>((_resolve, reject) => {
      const abortError = () => reject(new DOMException('The operation was aborted', 'AbortError'));
      if (signal?.aborted) {
        abortError();
        return;
      }
      signal?.addEventListener('abort', abortError, { once: true });
    });
  }

  function resolveDeferred(kind: DeferredKind, index: number): void {
    const entry = deferred.filter((item) => item.kind === kind)[index];
    if (!entry) {
      throw new Error(`No deferred ${kind} #${index}`);
    }
    entry.resolve(entry.response);
  }

  vi.mocked(fetch).mockImplementation(async (input, init) => {
    const url =
      typeof input === 'string' ? input : input instanceof Request ? input.url : String(input);
    const method = (init?.method ?? 'GET').toUpperCase();
    const body = init?.body ? JSON.parse(String(init.body)) : null;

    if (url === '/api/outreach/contacts') {
      if (method === 'GET') {
        if (options.hangGet) return track(hang(init?.signal));
        getCallCount += 1;
        const isInitialLoad = getCallCount === 1;
        // Snapshot the server's current truth now (at request time), exactly
        // like a real server would — a later deferred resolve must still
        // reflect what was true on disk when the request was received, not
        // whatever has mutated by the time it's released.
        const active = contacts.filter((contact) => !contact.deleted_at && !contact.replied_at);
        const uncontacted = active
          .filter((contact) => !contact.contacted_at)
          .sort((a, b) => a.created_at.localeCompare(b.created_at) || a.id - b.id);
        const contacted = active
          .filter((contact) => contact.contacted_at)
          .sort(
            (a, b) =>
              (a.contacted_at ?? '').localeCompare(b.contacted_at ?? '') || a.id - b.id
          );
        const response = jsonResponse({
          contacts: [...uncontacted, ...contacted].map((contact) => ({
            id: contact.id,
            name: contact.name,
            contacted_at: contact.contacted_at,
            created_at: contact.created_at,
          })),
        });
        if (options.deferGetAfterFirst && !isInitialLoad) return track(defer('get', response));
        return track(response);
      }
      if (method === 'POST') {
        addCalls += 1;
        const name = String(body?.name ?? '').trim();
        if (!name) return track(jsonResponse({ detail: 'Name is required' }, 400));
        const contact: StoredContact = {
          id: nextId++,
          name,
          contacted_at: null,
          created_at: `2026-09-26T10:00:${String(nextId).padStart(2, '0')}+00:00`,
        };
        contacts.push(contact);
        const response = jsonResponse({ contact }, 201);
        if (options.deferAdd) return track(defer('add', response));
        return track(response);
      }
    }

    const match = url.match(/^\/api\/outreach\/contacts\/(\d+)(?:\/(contacted|replied))?$/);
    if (match) {
      const id = Number(match[1]);
      const action = match[2];
      const index = contacts.findIndex((contact) => contact.id === id);
      if (index === -1 || contacts[index].deleted_at) {
        return track(jsonResponse({ detail: 'Contact not found' }, 404));
      }
      const contact = contacts[index];

      if (method === 'PATCH') {
        const name = String(body?.name ?? '').trim();
        if (!name) return track(jsonResponse({ detail: 'Name is required' }, 400));
        if (contact.replied_at) {
          return track(jsonResponse({ detail: 'Invalid name or contact state' }, 409));
        }
        const callIndex = patchCalls.length;
        patchCalls.push({ id, name });

        const hangThis =
          typeof options.hangPatch === 'function' ? options.hangPatch(callIndex) : options.hangPatch;
        if (hangThis) return track(hang(init?.signal));

        const networkErrorThis =
          typeof options.patchNetworkError === 'function'
            ? options.patchNetworkError(callIndex)
            : options.patchNetworkError;
        if (networkErrorThis) return track(Promise.reject(new Error('Network down')));

        // Simulates a concurrent delete/reply from another client: the
        // conflict response AND the row's removal from the server's own
        // store happen together, so a later GET genuinely omits it instead
        // of the test asserting a mock that never actually changed state.
        const deletedElsewhereThis =
          typeof options.patchDeletedElsewhere === 'function'
            ? options.patchDeletedElsewhere(callIndex)
            : options.patchDeletedElsewhere;
        if (deletedElsewhereThis) {
          contact.deleted_at = '2026-09-26T11:30:00+00:00';
          return track(jsonResponse({ detail: 'Contact not found' }, 404));
        }
        const repliedElsewhereThis =
          typeof options.patchRepliedElsewhere === 'function'
            ? options.patchRepliedElsewhere(callIndex)
            : options.patchRepliedElsewhere;
        if (repliedElsewhereThis) {
          contact.replied_at = '2026-09-26T11:30:00+00:00';
          return track(jsonResponse({ detail: 'Invalid name or contact state' }, 409));
        }

        const failThis =
          typeof options.patchFailure === 'function' ? options.patchFailure(callIndex) : options.patchFailure;
        if (failThis) {
          const response = jsonResponse({ detail: 'Failed to rename contact' }, 500);
          if (options.deferPatch) return track(defer('patch', response));
          return track(response);
        }

        contact.name = name;
        if (options.deferPatch) {
          // Snapshot the row exactly as the rename response would look when the
          // server processed it: the new name, still uncontacted. The caller
          // decides when this response lands, simulating a slow PATCH racing a
          // fast Contacted/Delete request.
          const response = jsonResponse({ contact: { ...contact } }, 200);
          return track(defer('patch', response));
        }
        return track(jsonResponse({ contact }, 200));
      }
      if (method === 'DELETE') {
        deleteCalls.push(id);
        contact.deleted_at = '2026-09-26T11:00:00+00:00';
        const response = jsonResponse({ contact }, 200);
        if (options.deferDelete) return track(defer('delete', response));
        return track(response);
      }
      if (method === 'POST' && action === 'contacted') {
        contactedCalls.push(id);
        const hangRequest =
          typeof options.hangContacted === 'function'
            ? options.hangContacted(id)
            : options.hangContacted;
        if (hangRequest) return track(hang(init?.signal));
        const networkError =
          typeof options.contactedNetworkError === 'function'
            ? options.contactedNetworkError(id)
            : options.contactedNetworkError;
        if (networkError) {
          return track(Promise.reject(new Error('Network down')));
        }
        const conflict =
          typeof options.contactedConflict === 'function'
            ? options.contactedConflict(id)
            : options.contactedConflict;
        if (conflict) {
          contact.contacted_at = '2026-09-26T11:00:00+00:00';
          return track(jsonResponse({ detail: 'Contact cannot be marked contacted' }, 409));
        }
        if (contact.contacted_at || contact.replied_at) {
          return track(jsonResponse({ detail: 'Contact cannot be marked contacted' }, 409));
        }
        contact.contacted_at = '2026-09-26T11:00:00+00:00';
        const response = jsonResponse({ contact }, 200);
        if (options.deferContacted) return track(defer('contacted', response));
        return track(response);
      }
      if (method === 'POST' && action === 'replied') {
        repliedCalls.push(id);
        if (!contact.contacted_at) {
          return track(jsonResponse({ detail: 'Contact cannot be marked replied' }, 409));
        }
        contact.replied_at = '2026-09-26T12:00:00+00:00';
        const response = jsonResponse({ contact }, 200);
        if (options.deferReplied) return track(defer('replied', response));
        return track(response);
      }
    }

    throw new Error(`Unexpected fetch: ${method} ${url}`);
  });

  return {
    patchCalls,
    contactedCalls,
    repliedCalls,
    deleteCalls,
    get addCalls() {
      return addCalls;
    },
    get maxInFlight() {
      return maxInFlight;
    },
    resolvePatch: (index = 0) => resolveDeferred('patch', index),
    resolve: (kind, index = 0) => resolveDeferred(kind, index),
  };
}

function isBefore(element: Element, other: Element): boolean {
  return Boolean(element.compareDocumentPosition(other) & Node.DOCUMENT_POSITION_FOLLOWING);
}

function uncontactedList(): HTMLElement {
  return screen.getByRole('list', { name: 'Not contacted' });
}

function contactedList(): HTMLElement {
  return screen.getByRole('list', { name: 'Contacted' });
}

function rowOf(name: string): HTMLElement {
  const row = screen.getByText(name).closest('li');
  if (!row) throw new Error(`No row for ${name}`);
  return row;
}

function fetchUrl(input: RequestInfo | URL): string {
  return typeof input === 'string' ? input : input instanceof Request ? input.url : String(input);
}

function getCallCount(): number {
  return vi.mocked(fetch).mock.calls.filter(([input, init]) => {
    return (
      fetchUrl(input) === '/api/outreach/contacts' &&
      (init?.method ?? 'GET').toUpperCase() === 'GET'
    );
  }).length;
}

async function flushAsync(): Promise<void> {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
}

describe('OutreachPanel', () => {
  beforeEach(() => {
    vi.resetAllMocks();
    window.localStorage.clear();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('adds a contact via the Add button above the input', async () => {
    mockOutreachApi([]);
    render(<OutreachPanel />);

    const input = (await screen.findByLabelText('Name or email')) as HTMLInputElement;
    fireEvent.change(input, { target: { value: ' Alice ' } });
    fireEvent.click(screen.getByRole('button', { name: 'Add' }));

    await waitFor(() => {
      expect(within(uncontactedList()).getByText('Alice')).toBeInTheDocument();
    });
    expect(input.value).toBe('');
    expect(isBefore(uncontactedList(), input)).toBe(true);
    expect(
      vi.mocked(fetch).mock.calls.some(
        ([, init]) => (init?.method ?? 'GET').toUpperCase() === 'POST'
      )
    ).toBe(true);
  });

  it('adds a contact when Enter is pressed in the input', async () => {
    const user = userEvent.setup();
    mockOutreachApi([]);
    render(<OutreachPanel />);

    const input = (await screen.findByLabelText('Name or email')) as HTMLInputElement;
    await user.type(input, 'Bob{enter}');

    await waitFor(() => {
      expect(within(uncontactedList()).getByText('Bob')).toBeInTheDocument();
    });
    expect(input.value).toBe('');
  });

  it('keeps uncontacted rows above the input and contacted rows below it', async () => {
    mockOutreachApi([
      { id: 1, name: 'Alice', contacted_at: null, created_at: '2026-09-26T09:00:00+00:00' },
      { id: 2, name: 'Bob', contacted_at: '2026-09-26T10:00:00+00:00', created_at: '2026-09-26T08:00:00+00:00' },
    ]);
    render(<OutreachPanel />);

    await screen.findByText('Alice');
    const input = screen.getByLabelText('Name or email');

    expect(within(uncontactedList()).getByText('Alice')).toBeInTheDocument();
    expect(within(contactedList()).getByText('Bob')).toBeInTheDocument();
    expect(isBefore(uncontactedList(), input)).toBe(true);
    expect(isBefore(input, contactedList())).toBe(true);
  });

  it('edits a name in place and saves on blur via PATCH', async () => {
    mockOutreachApi([
      { id: 1, name: 'Alice', contacted_at: null, created_at: '2026-09-26T09:00:00+00:00' },
    ]);
    render(<OutreachPanel />);

    await screen.findByText('Alice');
    fireEvent.click(screen.getByRole('button', { name: 'Alice' }));

    const editInput = screen.getByLabelText('Edit name for Alice') as HTMLInputElement;
    expect(document.activeElement).toBe(editInput);
    expect(editInput.value).toBe('Alice');
    expect(editInput.selectionStart).toBe(0);
    expect(editInput.selectionEnd).toBe('Alice'.length);

    fireEvent.change(editInput, { target: { value: 'Alicia' } });
    fireEvent.blur(editInput);

    await waitFor(() => {
      expect(screen.getByText('Alicia')).toBeInTheDocument();
    });
    expect(screen.queryByLabelText('Edit name for Alice')).not.toBeInTheDocument();
    expect(
      vi.mocked(fetch).mock.calls.some(
        ([, init]) => (init?.method ?? 'GET').toUpperCase() === 'PATCH'
      )
    ).toBe(true);
  });

  it('edits a contacted name in place and saves on blur, keeping it contacted', async () => {
    const user = userEvent.setup();
    const api = mockOutreachApi([
      {
        id: 1,
        name: 'Sabina',
        contacted_at: '2026-09-28T14:30:00+00:00',
        created_at: '2026-09-28T14:24:00+00:00',
      },
    ]);
    render(<OutreachPanel />);

    await user.click(await screen.findByRole('button', { name: 'Sabina' }));

    const editInput = screen.getByLabelText('Edit name for Sabina') as HTMLInputElement;
    expect(document.activeElement).toBe(editInput);
    expect(within(contactedList()).getByLabelText('Edit name for Sabina')).toBe(editInput);
    await user.clear(editInput);
    await user.type(editInput, 'Sabina Park');
    await user.tab();

    await waitFor(() => {
      expect(within(contactedList()).getByRole('button', { name: 'Sabina Park' })).toBeInTheDocument();
    });
    expect(screen.queryByLabelText('Edit name for Sabina')).not.toBeInTheDocument();
    expect(api.patchCalls).toEqual([{ id: 1, name: 'Sabina Park' }]);
    expect(within(contactedList()).getByRole('button', { name: 'Replied' })).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByRole('status')).toHaveTextContent('Saved');
    });
  });

  it('types a full rename character by character and saves it on blur', async () => {
    const user = userEvent.setup();
    mockOutreachApi([
      { id: 1, name: 'Alice', contacted_at: null, created_at: '2026-09-26T09:00:00+00:00' },
    ]);
    render(<OutreachPanel />);

    await screen.findByText('Alice');
    await user.click(screen.getByRole('button', { name: 'Alice' }));

    const editInput = screen.getByLabelText('Edit name for Alice') as HTMLInputElement;
    expect(editInput.selectionStart).toBe(0);
    expect(editInput.selectionEnd).toBe('Alice'.length);

    // Clear the pre-selected text, then type character by character. The old
    // inline-ref bug re-selected the value after every keystroke, so each
    // character replaced the previous one and only the last character survived.
    await user.clear(editInput);
    await user.type(editInput, 'Alpha Renamed');
    await user.tab();

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Alpha Renamed' })).toBeInTheDocument();
    });
    const patchCalls = vi.mocked(fetch).mock.calls.filter(
      ([, init]) => (init?.method ?? 'GET').toUpperCase() === 'PATCH'
    );
    expect(patchCalls).toHaveLength(1);
    const [, patchInit] = patchCalls[0];
    expect(JSON.parse(String(patchInit?.body))).toEqual({ name: 'Alpha Renamed' });
  });

  it('keeps both characters when typing right after entering edit mode', async () => {
    const user = userEvent.setup();
    mockOutreachApi([
      { id: 1, name: 'Alice', contacted_at: null, created_at: '2026-09-26T09:00:00+00:00' },
    ]);
    render(<OutreachPanel />);

    await screen.findByText('Alice');
    await user.click(screen.getByRole('button', { name: 'Alice' }));

    const editInput = screen.getByLabelText('Edit name for Alice') as HTMLInputElement;
    await user.clear(editInput);
    await user.type(editInput, 'xy');

    expect(editInput.value).toBe('xy');
  });

  it('cancels the edit on Escape and restores the old value', async () => {
    mockOutreachApi([
      { id: 1, name: 'Alice', contacted_at: null, created_at: '2026-09-26T09:00:00+00:00' },
    ]);
    render(<OutreachPanel />);

    await screen.findByText('Alice');
    fireEvent.click(screen.getByRole('button', { name: 'Alice' }));

    const editInput = screen.getByLabelText('Edit name for Alice') as HTMLInputElement;
    fireEvent.change(editInput, { target: { value: 'Changed' } });
    fireEvent.keyDown(editInput, { key: 'Escape' });

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Alice' })).toBeInTheDocument();
    });
    expect(screen.queryByLabelText('Edit name for Alice')).not.toBeInTheDocument();
    expect(
      vi.mocked(fetch).mock.calls.some(
        ([, init]) => (init?.method ?? 'GET').toUpperCase() === 'PATCH'
      )
    ).toBe(false);
  });

  it('rejects an empty rename on blur and restores the old value', async () => {
    mockOutreachApi([
      { id: 1, name: 'Alice', contacted_at: null, created_at: '2026-09-26T09:00:00+00:00' },
    ]);
    render(<OutreachPanel />);

    await screen.findByText('Alice');
    fireEvent.click(screen.getByRole('button', { name: 'Alice' }));

    const editInput = screen.getByLabelText('Edit name for Alice') as HTMLInputElement;
    fireEvent.change(editInput, { target: { value: '   ' } });
    fireEvent.blur(editInput);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Alice' })).toBeInTheDocument();
    });
    expect(
      vi.mocked(fetch).mock.calls.some(
        ([, init]) => (init?.method ?? 'GET').toUpperCase() === 'PATCH'
      )
    ).toBe(false);
  });

  it('serializes rename and Contacted so a late PATCH response cannot revert the row', async () => {
    const user = userEvent.setup();
    const api = mockOutreachApi(
      [
        { id: 1, name: 'Alice', contacted_at: null, created_at: '2026-09-26T09:00:00+00:00' },
      ],
      { deferPatch: true }
    );
    render(<OutreachPanel />);

    await screen.findByText('Alice');
    await user.click(screen.getByRole('button', { name: 'Alice' }));

    const editInput = screen.getByLabelText('Edit name for Alice') as HTMLInputElement;
    await user.clear(editInput);
    await user.type(editInput, 'Alicia');
    // Clicking Contacted blurs the edit input first, so the PATCH rename and
    // the POST /contacted fire back to back. The POST response resolves
    // immediately; the PATCH response stays pending until the end.
    await user.click(screen.getByRole('button', { name: 'Contacted' }));

    await waitFor(() => {
      expect(api.patchCalls).toHaveLength(1);
    });
    expect(api.patchCalls[0].name).toBe('Alicia');

    // The POST /contacted must wait for the pending rename instead of racing it.
    expect(api.contactedCalls).toHaveLength(0);

    // The slow PATCH (still uncontacted, with the new name) lands last.
    api.resolvePatch();

    await waitFor(() => {
      expect(api.contactedCalls).toHaveLength(1);
      expect(within(contactedList()).getByText('Alicia')).toBeInTheDocument();
      expect(screen.getByRole('button', { name: 'Replied' })).toBeInTheDocument();
    });
    expect(screen.queryByText('Alice')).not.toBeInTheDocument();
    const row = within(contactedList()).getByText('Alicia').closest('li');
    expect(row).toHaveClass('outreach-row--contacted');
  });

  it('serializes rename and Delete so a late PATCH response cannot resurrect the row', async () => {
    const user = userEvent.setup();
    const api = mockOutreachApi(
      [
        { id: 1, name: 'Alice', contacted_at: null, created_at: '2026-09-26T09:00:00+00:00' },
      ],
      { deferPatch: true }
    );
    render(<OutreachPanel />);

    await screen.findByText('Alice');
    await user.click(screen.getByRole('button', { name: 'Alice' }));

    const editInput = screen.getByLabelText('Edit name for Alice') as HTMLInputElement;
    await user.clear(editInput);
    await user.type(editInput, 'Alicia');
    await user.click(screen.getByRole('button', { name: 'Delete' }));

    await waitFor(() => {
      expect(api.patchCalls).toHaveLength(1);
    });
    expect(api.patchCalls[0].name).toBe('Alicia');

    // The DELETE must wait for the pending rename instead of racing it.
    expect(api.deleteCalls).toHaveLength(0);
    api.resolvePatch();

    await waitFor(() => {
      expect(api.deleteCalls).toHaveLength(1);
      expect(screen.queryByText('Alicia')).not.toBeInTheDocument();
    });
    expect(screen.queryByText('Alice')).not.toBeInTheDocument();
  });

  it('moves a row below the input and shows Replied after Contacted', async () => {
    mockOutreachApi([
      { id: 1, name: 'Alice', contacted_at: null, created_at: '2026-09-26T09:00:00+00:00' },
    ]);
    render(<OutreachPanel />);

    await screen.findByText('Alice');
    fireEvent.click(screen.getByRole('button', { name: 'Contacted' }));

    await waitFor(() => {
      expect(within(contactedList()).getByText('Alice')).toBeInTheDocument();
    });
    expect(screen.getByRole('button', { name: 'Replied' })).toBeInTheDocument();
    expect(screen.queryByRole('list', { name: 'Not contacted' })).not.toBeInTheDocument();
  });

  it('removes the row when Replied is clicked', async () => {
    mockOutreachApi([
      { id: 1, name: 'Alice', contacted_at: '2026-09-26T10:00:00+00:00', created_at: '2026-09-26T09:00:00+00:00' },
    ]);
    render(<OutreachPanel />);

    await screen.findByText('Alice');
    fireEvent.click(screen.getByRole('button', { name: 'Replied' }));

    await waitFor(() => {
      expect(screen.queryByText('Alice')).not.toBeInTheDocument();
    });
  });

  it('removes the row when Delete is clicked', async () => {
    mockOutreachApi([
      { id: 1, name: 'Alice', contacted_at: null, created_at: '2026-09-26T09:00:00+00:00' },
    ]);
    render(<OutreachPanel />);

    await screen.findByText('Alice');
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }));

    await waitFor(() => {
      expect(screen.queryByText('Alice')).not.toBeInTheDocument();
    });
  });

  it('shows an error and keeps the list when an action fails', async () => {
    mockOutreachApi([
      { id: 1, name: 'Alice', contacted_at: null, created_at: '2026-09-26T09:00:00+00:00' },
    ]);
    render(<OutreachPanel />);

    await screen.findByText('Alice');
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: false,
      status: 500,
      json: async () => ({ detail: 'Failed to add contact' }),
    } as Response);

    fireEvent.change(screen.getByLabelText('Name or email'), { target: { value: 'Bob' } });
    fireEvent.click(screen.getByRole('button', { name: 'Add' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('Failed to add contact');
    expect(screen.getByText('Alice')).toBeInTheDocument();
  });

  it('still renders the input when the initial load fails', async () => {
    vi.mocked(fetch).mockResolvedValueOnce({
      ok: false,
      status: 500,
      json: async () => ({ detail: 'Failed to load contacts' }),
    } as Response);
    render(<OutreachPanel />);

    expect(await screen.findByRole('alert')).toHaveTextContent('Failed to load contacts');
    expect(screen.getByLabelText('Name or email')).toBeInTheDocument();
  });

  it('minimizes and restores from localStorage', async () => {
    mockOutreachApi([]);
    const { unmount } = render(<OutreachPanel />);

    await screen.findByLabelText('Name or email');
    fireEvent.click(screen.getByRole('button', { name: /People to contact/ }));

    await waitFor(() => {
      expect(screen.queryByLabelText('Name or email')).not.toBeInTheDocument();
    });
    expect(window.localStorage.getItem('outreach-panel:minimized')).toBe('1');

    unmount();
    render(<OutreachPanel />);

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /People to contact/ })).toHaveAttribute(
        'aria-expanded',
        'false'
      );
    });
    expect(screen.queryByLabelText('Name or email')).not.toBeInTheDocument();
  });

  it('sends exactly one POST /contacted when Contacted is double-clicked', async () => {
    const user = userEvent.setup();
    const api = mockOutreachApi(
      [
        { id: 1, name: 'Alice', contacted_at: null, created_at: '2026-09-26T09:00:00+00:00' },
      ],
      { deferContacted: true }
    );
    render(<OutreachPanel />);

    await screen.findByText('Alice');
    await user.dblClick(screen.getByRole('button', { name: 'Contacted' }));

    await waitFor(() => {
      expect(api.contactedCalls).toHaveLength(1);
    });
    expect(screen.getByRole('button', { name: 'Contacted' })).toBeDisabled();

    api.resolve('contacted');

    await waitFor(() => {
      expect(within(contactedList()).getByText('Alice')).toBeInTheDocument();
    });
    expect(api.contactedCalls).toHaveLength(1);
  });

  it('sends exactly one DELETE when Delete is double-clicked', async () => {
    const user = userEvent.setup();
    const api = mockOutreachApi(
      [
        { id: 1, name: 'Alice', contacted_at: null, created_at: '2026-09-26T09:00:00+00:00' },
      ],
      { deferDelete: true }
    );
    render(<OutreachPanel />);

    await screen.findByText('Alice');
    await user.dblClick(screen.getByRole('button', { name: 'Delete' }));

    await waitFor(() => {
      expect(api.deleteCalls).toHaveLength(1);
    });
    expect(screen.getByRole('button', { name: 'Delete' })).toBeDisabled();

    api.resolve('delete');

    await waitFor(() => {
      expect(screen.queryByText('Alice')).not.toBeInTheDocument();
    });
    expect(api.deleteCalls).toHaveLength(1);
  });

  it('sends exactly one POST /replied when Replied is double-clicked', async () => {
    const user = userEvent.setup();
    const api = mockOutreachApi(
      [
        {
          id: 1,
          name: 'Alice',
          contacted_at: '2026-09-26T10:00:00+00:00',
          created_at: '2026-09-26T09:00:00+00:00',
        },
      ],
      { deferReplied: true }
    );
    render(<OutreachPanel />);

    await screen.findByText('Alice');
    await user.dblClick(screen.getByRole('button', { name: 'Replied' }));

    await waitFor(() => {
      expect(api.repliedCalls).toHaveLength(1);
    });
    expect(screen.getByRole('button', { name: 'Replied' })).toBeDisabled();

    api.resolve('replied');

    await waitFor(() => {
      expect(screen.queryByText('Alice')).not.toBeInTheDocument();
    });
    expect(api.repliedCalls).toHaveLength(1);
  });

  it('shows the error and re-renders the server list when Contacted returns 409', async () => {
    const api = mockOutreachApi(
      [
        { id: 1, name: 'Alice', contacted_at: null, created_at: '2026-09-26T09:00:00+00:00' },
      ],
      { contactedConflict: true }
    );
    render(<OutreachPanel />);

    await screen.findByText('Alice');
    fireEvent.click(screen.getByRole('button', { name: 'Contacted' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Contact cannot be marked contacted'
    );
    await waitFor(() => {
      expect(within(contactedList()).getByText('Alice')).toBeInTheDocument();
    });
    expect(screen.queryByRole('list', { name: 'Not contacted' })).not.toBeInTheDocument();
    expect(api.contactedCalls).toHaveLength(1);
  });

  it('disables a row\'s action buttons while its request is pending', async () => {
    const api = mockOutreachApi(
      [
        { id: 1, name: 'Alice', contacted_at: null, created_at: '2026-09-26T09:00:00+00:00' },
      ],
      { deferContacted: true }
    );
    render(<OutreachPanel />);

    await screen.findByText('Alice');
    fireEvent.click(screen.getByRole('button', { name: 'Contacted' }));

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Contacted' })).toBeDisabled();
    });
    expect(screen.getByRole('button', { name: 'Delete' })).toBeDisabled();

    api.resolve('contacted');

    await waitFor(() => {
      expect(within(contactedList()).getByText('Alice')).toBeInTheDocument();
    });
  });

  it('cannot start editing a row while its request is in flight', async () => {
    const api = mockOutreachApi(
      [
        { id: 1, name: 'Alice', contacted_at: null, created_at: '2026-09-26T09:00:00+00:00' },
      ],
      { deferContacted: true }
    );
    render(<OutreachPanel />);

    await screen.findByText('Alice');
    fireEvent.click(screen.getByRole('button', { name: 'Contacted' }));

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Alice' })).toBeDisabled();
    });
    fireEvent.click(screen.getByRole('button', { name: 'Alice' }));
    expect(screen.queryByLabelText('Edit name for Alice')).not.toBeInTheDocument();

    api.resolve('contacted');

    await waitFor(() => {
      expect(within(contactedList()).getByText('Alice')).toBeInTheDocument();
    });
  });

  it('disables Add while an add is in flight and double-clicking creates exactly one POST', async () => {
    const user = userEvent.setup();
    const api = mockOutreachApi([], { deferAdd: true });
    render(<OutreachPanel />);

    const input = (await screen.findByLabelText('Name or email')) as HTMLInputElement;
    fireEvent.change(input, { target: { value: 'Alice' } });
    await user.dblClick(screen.getByRole('button', { name: 'Add' }));

    await waitFor(() => {
      expect(api.addCalls).toBe(1);
    });
    expect(screen.getByRole('button', { name: 'Add' })).toBeDisabled();

    api.resolve('add');

    await waitFor(() => {
      expect(within(uncontactedList()).getByText('Alice')).toBeInTheDocument();
    });
    expect(api.addCalls).toBe(1);
    expect(input.value).toBe('');
  });

  it('serializes Contacted across rows: a later failure cannot resync over an earlier success', async () => {
    const api = mockOutreachApi(
      [
        { id: 1, name: 'Alice', contacted_at: null, created_at: '2026-09-26T09:00:00+00:00' },
        { id: 2, name: 'Bob', contacted_at: null, created_at: '2026-09-26T10:00:00+00:00' },
      ],
      { deferContacted: true, contactedConflict: (id) => id === 1 }
    );
    render(<OutreachPanel />);

    await screen.findByText('Alice');
    fireEvent.click(within(rowOf('Bob')).getByRole('button', { name: 'Contacted' }));
    await waitFor(() => {
      expect(api.contactedCalls).toEqual([2]);
    });

    fireEvent.click(within(rowOf('Alice')).getByRole('button', { name: 'Contacted' }));
    // Flush microtasks so a concurrent implementation would reveal its second POST.
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    // B's POST is still pending, so A's POST must not be sent yet.
    expect(api.contactedCalls).toEqual([2]);

    api.resolve('contacted', 0);

    await waitFor(() => {
      expect(api.contactedCalls).toEqual([2, 1]);
    });
    await waitFor(() => {
      expect(within(contactedList()).getByText('Bob')).toBeInTheDocument();
      expect(within(contactedList()).getByText('Alice')).toBeInTheDocument();
    });
    expect(screen.queryByRole('list', { name: 'Not contacted' })).not.toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('Contact cannot be marked contacted');
  });

  it('runs exactly one request at a time and ends on the server state', async () => {
    const user = userEvent.setup();
    const api = mockOutreachApi(
      [
        { id: 1, name: 'Alice', contacted_at: null, created_at: '2026-09-26T09:00:00+00:00' },
        { id: 2, name: 'Bob', contacted_at: null, created_at: '2026-09-26T10:00:00+00:00' },
      ],
      { deferPatch: true, deferContacted: true, deferDelete: true }
    );
    render(<OutreachPanel />);

    await screen.findByText('Alice');
    const aliceRow = rowOf('Alice');
    const bobRow = rowOf('Bob');

    await user.click(within(aliceRow).getByRole('button', { name: 'Alice' }));
    const editInput = screen.getByLabelText('Edit name for Alice') as HTMLInputElement;
    await user.clear(editInput);
    await user.type(editInput, 'Alicia');
    // Blur fires before the click, so the PATCH rename is enqueued ahead of the
    // Contacted POST; the Delete on Bob is enqueued behind both.
    await user.click(within(aliceRow).getByRole('button', { name: 'Contacted' }));
    await user.click(within(bobRow).getByRole('button', { name: 'Delete' }));

    await waitFor(() => {
      expect(api.patchCalls).toHaveLength(1);
    });
    expect(api.contactedCalls).toHaveLength(0);
    expect(api.deleteCalls).toHaveLength(0);
    expect(api.maxInFlight).toBe(1);

    api.resolvePatch();
    await waitFor(() => {
      expect(api.contactedCalls).toHaveLength(1);
    });
    expect(api.deleteCalls).toHaveLength(0);
    expect(api.maxInFlight).toBe(1);

    api.resolve('contacted');
    await waitFor(() => {
      expect(api.deleteCalls).toHaveLength(1);
    });
    expect(api.maxInFlight).toBe(1);

    api.resolve('delete');
    await waitFor(() => {
      expect(within(contactedList()).getByText('Alicia')).toBeInTheDocument();
      expect(screen.queryByText('Bob')).not.toBeInTheDocument();
    });
    expect(api.maxInFlight).toBe(1);
  });

  it('still runs a queued action after its predecessor fails', async () => {
    const api = mockOutreachApi(
      [
        { id: 1, name: 'Alice', contacted_at: null, created_at: '2026-09-26T09:00:00+00:00' },
        { id: 2, name: 'Bob', contacted_at: null, created_at: '2026-09-26T10:00:00+00:00' },
      ],
      { contactedNetworkError: true, deferDelete: true }
    );
    render(<OutreachPanel />);

    await screen.findByText('Alice');
    fireEvent.click(within(rowOf('Alice')).getByRole('button', { name: 'Contacted' }));
    fireEvent.click(within(rowOf('Bob')).getByRole('button', { name: 'Delete' }));

    // The first request fails and surfaces its error...
    await waitFor(() => {
      expect(api.contactedCalls).toEqual([1]);
    });
    expect(await screen.findByRole('alert')).toHaveTextContent('Network down');

    // ...and the queued DELETE still runs instead of breaking the chain.
    await waitFor(() => {
      expect(api.deleteCalls).toEqual([2]);
    });
    api.resolve('delete');
    await waitFor(() => {
      expect(screen.queryByText('Bob')).not.toBeInTheDocument();
    });
    expect(screen.getByText('Alice')).toBeInTheDocument();
  });

  it('shows a Saving indicator only while a request is queued or in flight', async () => {
    const api = mockOutreachApi(
      [
        { id: 1, name: 'Alice', contacted_at: null, created_at: '2026-09-26T09:00:00+00:00' },
      ],
      { deferContacted: true }
    );
    render(<OutreachPanel />);

    await screen.findByText('Alice');
    await waitFor(() => {
      expect(screen.queryByText('Saving…')).not.toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: 'Contacted' }));
    await waitFor(() => {
      expect(api.contactedCalls).toHaveLength(1);
    });
    expect(screen.getByText('Saving…')).toBeInTheDocument();

    api.resolve('contacted');
    await waitFor(() => {
      expect(within(contactedList()).getByText('Alice')).toBeInTheDocument();
    });
    await waitFor(() => {
      expect(screen.queryByText('Saving…')).not.toBeInTheDocument();
    });
  });

  it('times out a hung mutation, resyncs, and lets the queue continue', async () => {
    vi.useFakeTimers();
    const api = mockOutreachApi(
      [
        { id: 1, name: 'Alice', contacted_at: null, created_at: '2026-09-26T09:00:00+00:00' },
        { id: 2, name: 'Bob', contacted_at: null, created_at: '2026-09-26T10:00:00+00:00' },
      ],
      { hangContacted: true, deferDelete: true }
    );

    await act(async () => {
      render(<OutreachPanel />);
    });
    await flushAsync();
    expect(screen.getByText('Alice')).toBeInTheDocument();
    expect(screen.getByText('Bob')).toBeInTheDocument();

    fireEvent.click(within(rowOf('Alice')).getByRole('button', { name: 'Contacted' }));
    fireEvent.click(within(rowOf('Bob')).getByRole('button', { name: 'Delete' }));
    await flushAsync();

    // Row 1's POST is hung and row 2's DELETE is queued behind it.
    expect(api.contactedCalls).toEqual([1]);
    expect(api.deleteCalls).toEqual([]);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(OUTREACH_REQUEST_TIMEOUT_MS);
    });

    // The hung POST timed out and surfaced the timeout error.
    expect(screen.getByRole('alert')).toHaveTextContent('Request timed out — reloading contacts');

    // The queued DELETE was dispatched, so the queue was not blocked.
    expect(api.deleteCalls).toEqual([2]);
    expect(screen.getByText('Saving…')).toBeInTheDocument();

    api.resolve('delete');
    await flushAsync();

    // The resync enqueued by the failed mutation ran, restoring server truth.
    expect(getCallCount()).toBe(2);
    expect(screen.queryByText('Bob')).not.toBeInTheDocument();
    expect(screen.getByText('Alice')).toBeInTheDocument();
    expect(screen.queryByText('Saving…')).not.toBeInTheDocument();
  });

  it('shows an error and keeps the Add form usable when the initial load times out', async () => {
    vi.useFakeTimers();
    mockOutreachApi([], { hangGet: true });

    await act(async () => {
      render(<OutreachPanel />);
    });
    expect(screen.getByText('Loading contacts...')).toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(OUTREACH_REQUEST_TIMEOUT_MS);
    });

    expect(screen.getByRole('alert')).toHaveTextContent('Request timed out — reloading contacts');
    const input = screen.getByLabelText('Name or email') as HTMLInputElement;
    expect(input).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Add' })).not.toBeDisabled();

    // A later successful action still works and fills the list.
    fireEvent.change(input, { target: { value: 'Alice' } });
    fireEvent.click(screen.getByRole('button', { name: 'Add' }));
    await flushAsync();
    expect(within(uncontactedList()).getByText('Alice')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  // --- Per-field save-status indicator (#7717) ---
  // NOTE: the panel header already renders a queue-wide "Saving…" indicator
  // (outreach-saving) whenever any request is queued/in-flight, so every
  // assertion below scopes its query to the row (`within(row)`) to avoid
  // colliding with that unrelated, pre-existing indicator.

  function rowFor(buttonName: string): HTMLElement {
    const button = screen.getByRole('button', { name: buttonName }).closest('li');
    if (!button) throw new Error(`No row for button "${buttonName}"`);
    return button as HTMLElement;
  }

  it('shows "Saving…" only from commit until the response, then "Saved", then clears after the fade delay', async () => {
    vi.useFakeTimers();
    const api = mockOutreachApi(
      [{ id: 1, name: 'Alice', contacted_at: null, created_at: '2026-09-26T09:00:00+00:00' }],
      { deferPatch: true }
    );

    await act(async () => {
      render(<OutreachPanel />);
    });
    await flushAsync();

    fireEvent.click(screen.getByRole('button', { name: 'Alice' }));
    const editInput = screen.getByLabelText('Edit name for Alice') as HTMLInputElement;
    fireEvent.change(editInput, { target: { value: 'Alicia' } });
    fireEvent.blur(editInput);
    await flushAsync();

    const row = rowFor('Alicia');
    // "Saved" must never appear before the response lands.
    expect(within(row).getByText('Saving…')).toBeInTheDocument();
    expect(within(row).queryByText('Saved')).not.toBeInTheDocument();

    api.resolvePatch();
    await flushAsync();

    expect(within(row).getByText('Saved')).toBeInTheDocument();
    expect(within(row).queryByText('Saving…')).not.toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });

    expect(within(row).queryByText('Saved')).not.toBeInTheDocument();
    expect(within(row).queryByText('Saving…')).not.toBeInTheDocument();
  });

  it('shows a retry control on 500 failure, keeps the typed value, and recovers when retried', async () => {
    vi.useFakeTimers();
    const api = mockOutreachApi(
      [{ id: 1, name: 'Alice', contacted_at: null, created_at: '2026-09-26T09:00:00+00:00' }],
      { patchFailure: (callIndex) => callIndex === 0 }
    );

    await act(async () => {
      render(<OutreachPanel />);
    });
    await flushAsync();

    fireEvent.click(screen.getByRole('button', { name: 'Alice' }));
    const editInput = screen.getByLabelText('Edit name for Alice') as HTMLInputElement;
    fireEvent.change(editInput, { target: { value: 'Alicia' } });
    fireEvent.blur(editInput);
    await flushAsync();

    const row = rowFor('Alicia');
    expect(within(row).getByRole('button', { name: "Couldn't save — retry" })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Alicia' })).toBeInTheDocument(); // typed value survived, not reverted

    // The error persists well past the 3s "Saved" window — it never auto-clears.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(within(row).getByRole('button', { name: "Couldn't save — retry" })).toBeInTheDocument();

    fireEvent.click(within(row).getByRole('button', { name: "Couldn't save — retry" }));
    await flushAsync();

    expect(api.patchCalls).toHaveLength(2);
    expect(api.patchCalls[1]).toEqual({ id: 1, name: 'Alicia' });
    // Note: under fake timers, `waitFor`'s internal polling relies on real
    // timers and would hang forever here, so we assert directly once the
    // microtask queue (flushAsync) has settled instead.
    expect(within(row).getByText('Saved')).toBeInTheDocument();
  });

  it('shows a retry control on a network error and on a timeout', async () => {
    vi.useFakeTimers();
    mockOutreachApi(
      [
        { id: 1, name: 'Alice', contacted_at: null, created_at: '2026-09-26T09:00:00+00:00' },
        { id: 2, name: 'Bob', contacted_at: null, created_at: '2026-09-26T10:00:00+00:00' },
      ],
      { patchNetworkError: (callIndex) => callIndex === 0, hangPatch: (callIndex) => callIndex === 1 }
    );

    await act(async () => {
      render(<OutreachPanel />);
    });
    await flushAsync();

    fireEvent.click(screen.getByRole('button', { name: 'Alice' }));
    fireEvent.change(screen.getByLabelText('Edit name for Alice'), { target: { value: 'Alicia' } });
    fireEvent.blur(screen.getByLabelText('Edit name for Alice'));
    await flushAsync();
    expect(within(rowFor('Alicia')).getByRole('button', { name: "Couldn't save — retry" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Bob' }));
    fireEvent.change(screen.getByLabelText('Edit name for Bob'), { target: { value: 'Bobby' } });
    fireEvent.blur(screen.getByLabelText('Edit name for Bob'));
    await flushAsync();
    expect(within(rowFor('Bobby')).getByText('Saving…')).toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(OUTREACH_REQUEST_TIMEOUT_MS);
    });
    expect(within(rowFor('Bobby')).getByRole('button', { name: "Couldn't save — retry" })).toBeInTheDocument();
  });

  it('does not let a stale fade timer from an earlier save clobber a later save of the same field', async () => {
    // The panel-wide queue is strictly serial, so two PATCH responses for the
    // same field can never literally arrive out of order over the network —
    // the second request cannot even be sent until the first settles. The
    // real same-field ordering hazard is the 3s "Saved" fade/clear timer,
    // which runs on the wall clock independent of the queue: if a second save
    // of the same field starts before that timer fires, the stale timer must
    // not stomp the newer save's state. This test constructs exactly that:
    // save #1 succeeds and schedules its fade timers, save #2 starts (kept
    // pending) before they elapse, then we advance past when save #1's timers
    // would have fired and assert save #2's state was not clobbered.
    vi.useFakeTimers();
    const api = mockOutreachApi(
      [{ id: 1, name: 'Alice', contacted_at: null, created_at: '2026-09-26T09:00:00+00:00' }],
      { deferPatch: true }
    );

    await act(async () => {
      render(<OutreachPanel />);
    });
    await flushAsync();

    fireEvent.click(screen.getByRole('button', { name: 'Alice' }));
    fireEvent.change(screen.getByLabelText('Edit name for Alice'), { target: { value: 'Alicia' } });
    fireEvent.blur(screen.getByLabelText('Edit name for Alice'));
    await flushAsync();
    api.resolvePatch(0);
    await flushAsync();

    let row = rowFor('Alicia');
    expect(within(row).getByText('Saved')).toBeInTheDocument();

    // 1s into the 3s "Saved" window, start a second save of the same field.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    fireEvent.click(within(row).getByRole('button', { name: 'Alicia' }));
    fireEvent.change(within(row).getByLabelText('Edit name for Alicia'), { target: { value: 'Alicia Two' } });
    fireEvent.blur(within(row).getByLabelText('Edit name for Alicia'));
    await flushAsync();

    row = rowFor('Alicia Two');
    expect(within(row).getByText('Saving…')).toBeInTheDocument();

    // Advance past when save #1's fade/clear timers would have fired
    // (originally due at +3000ms from save #1, i.e. +2000ms from here).
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2500);
    });

    // Save #2's PATCH is still deferred/pending — the stale timer from save #1
    // must not have cleared or reverted it.
    expect(within(row).getByText('Saving…')).toBeInTheDocument();
    expect(within(row).getByRole('button', { name: 'Alicia Two' })).toBeInTheDocument();

    api.resolvePatch(1);
    await flushAsync();
    expect(within(row).getByText('Saved')).toBeInTheDocument();
  });

  it('click-away Contacted after a failed rename: Contacted still waits for the rename, and the contacted-list row shows the truthful retry state', async () => {
    const user = userEvent.setup();
    const api = mockOutreachApi(
      [{ id: 1, name: 'Alice', contacted_at: null, created_at: '2026-09-26T09:00:00+00:00' }],
      { patchFailure: true, deferPatch: true }
    );
    render(<OutreachPanel />);
    await screen.findByText('Alice');

    await user.click(screen.getByRole('button', { name: 'Alice' }));
    const editInput = screen.getByLabelText('Edit name for Alice') as HTMLInputElement;
    await user.clear(editInput);
    await user.type(editInput, 'Alicia');
    // user-event's click blurs the input first, so the PATCH is enqueued ahead
    // of the POST /contacted.
    await user.click(screen.getByRole('button', { name: 'Contacted' }));

    await waitFor(() => {
      expect(api.patchCalls).toHaveLength(1);
    });
    // Contacted must wait for the (failing, deliberately still-pending) rename
    // to settle before it fires.
    expect(api.contactedCalls).toHaveLength(0);

    api.resolvePatch();

    await waitFor(() => {
      expect(api.contactedCalls).toHaveLength(1);
    });
    await waitFor(() => {
      expect(within(contactedList()).getByText('Alicia')).toBeInTheDocument();
    });
    expect(screen.queryByRole('list', { name: 'Not contacted' })).not.toBeInTheDocument();
    const row = within(contactedList()).getByText('Alicia').closest('li') as HTMLElement;
    expect(within(row).getByRole('button', { name: "Couldn't save — retry" })).toBeInTheDocument();
  });

  it('warns before leaving while a save is in flight, and stops warning once it succeeds', async () => {
    const api = mockOutreachApi(
      [{ id: 1, name: 'Alice', contacted_at: null, created_at: '2026-09-26T09:00:00+00:00' }],
      { deferPatch: true }
    );
    render(<OutreachPanel />);
    await screen.findByText('Alice');

    function dispatchBeforeUnload(): Event {
      const event = new Event('beforeunload', { cancelable: true });
      window.dispatchEvent(event);
      return event;
    }

    expect(dispatchBeforeUnload().defaultPrevented).toBe(false);

    fireEvent.click(screen.getByRole('button', { name: 'Alice' }));
    fireEvent.change(screen.getByLabelText('Edit name for Alice'), { target: { value: 'Alicia' } });
    fireEvent.blur(screen.getByLabelText('Edit name for Alice'));
    await flushAsync();

    expect(dispatchBeforeUnload().defaultPrevented).toBe(true);

    api.resolvePatch();
    await flushAsync();

    // Succeeded — no longer warns, even while "Saved" is still visible.
    expect(dispatchBeforeUnload().defaultPrevented).toBe(false);
  });

  it('keeps warning before leaving the page while a rename has failed', async () => {
    const api = mockOutreachApi(
      [{ id: 1, name: 'Alice', contacted_at: null, created_at: '2026-09-26T09:00:00+00:00' }],
      { patchFailure: true }
    );
    render(<OutreachPanel />);
    await screen.findByText('Alice');

    fireEvent.click(screen.getByRole('button', { name: 'Alice' }));
    fireEvent.change(screen.getByLabelText('Edit name for Alice'), { target: { value: 'Alicia' } });
    fireEvent.blur(screen.getByLabelText('Edit name for Alice'));
    await waitFor(() => {
      expect(api.patchCalls).toHaveLength(1);
    });

    const event = new Event('beforeunload', { cancelable: true });
    window.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(true);
  });

  it('renders the status text in an aria-live region', async () => {
    const api = mockOutreachApi(
      [{ id: 1, name: 'Alice', contacted_at: null, created_at: '2026-09-26T09:00:00+00:00' }],
      { deferPatch: true }
    );
    render(<OutreachPanel />);
    await screen.findByText('Alice');

    fireEvent.click(screen.getByRole('button', { name: 'Alice' }));
    fireEvent.change(screen.getByLabelText('Edit name for Alice'), { target: { value: 'Alicia' } });
    fireEvent.blur(screen.getByLabelText('Edit name for Alice'));
    await flushAsync();

    const row = rowFor('Alicia');
    const savingText = within(row).getByText('Saving…');
    expect(savingText.closest('[aria-live="polite"]')).not.toBeNull();

    api.resolvePatch();
    await flushAsync();

    const savedText = within(row).getByText('Saved');
    expect(savedText.closest('[aria-live="polite"]')).not.toBeNull();
  });

  it('applies no fade class under prefers-reduced-motion — the text simply clears', async () => {
    vi.useFakeTimers();
    vi.stubGlobal(
      'matchMedia',
      vi.fn().mockReturnValue({
        matches: true,
        media: '(prefers-reduced-motion: reduce)',
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      })
    );

    const api = mockOutreachApi(
      [{ id: 1, name: 'Alice', contacted_at: null, created_at: '2026-09-26T09:00:00+00:00' }],
      { deferPatch: true }
    );

    await act(async () => {
      render(<OutreachPanel />);
    });
    await flushAsync();

    fireEvent.click(screen.getByRole('button', { name: 'Alice' }));
    fireEvent.change(screen.getByLabelText('Edit name for Alice'), { target: { value: 'Alicia' } });
    fireEvent.blur(screen.getByLabelText('Edit name for Alice'));
    await flushAsync();
    api.resolvePatch();
    await flushAsync();

    const row = rowFor('Alicia');
    expect(within(row).getByText('Saved').className).not.toMatch(/fade/);

    // Right up to (but not past) the clear point: under reduced motion there
    // is no separate fade sub-state, so the class never appears at any point.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2900);
    });
    expect(within(row).getByText('Saved').className).not.toMatch(/fade/);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(200);
    });
    expect(within(row).queryByText('Saved')).not.toBeInTheDocument();

    vi.unstubAllGlobals();
  });

  // --- resyncAfterError message separation (#7721) ---

  it('separates the action error and the resync error into two sentences', async () => {
    mockOutreachApi([
      { id: 1, name: 'Alice', contacted_at: null, created_at: '2026-09-26T09:00:00+00:00' },
    ]);
    render(<OutreachPanel />);
    await screen.findByText('Alice');

    // Both the Contacted POST and the resync GET it triggers fail the same
    // way a real `fetch` rejects: "Failed to fetch", with no punctuation of
    // its own. The combined banner must read as two sentences, not
    // "Failed to fetch Could not reload contacts: Failed to fetch".
    vi.mocked(fetch)
      .mockImplementationOnce(async () => {
        throw new Error('Failed to fetch');
      })
      .mockImplementationOnce(async () => {
        throw new Error('Failed to fetch');
      });

    fireEvent.click(screen.getByRole('button', { name: 'Contacted' }));

    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent(
        'Failed to fetch. Could not reload contacts: Failed to fetch.'
      );
    });
  });

  it('does not double a period when the first error message already ends with one', async () => {
    mockOutreachApi([
      { id: 1, name: 'Alice', contacted_at: null, created_at: '2026-09-26T09:00:00+00:00' },
    ]);
    render(<OutreachPanel />);
    await screen.findByText('Alice');

    vi.mocked(fetch)
      .mockResolvedValueOnce(jsonResponse({ detail: 'Failed to mark contact.' }, 500))
      .mockImplementationOnce(async () => {
        throw new Error('Failed to fetch');
      });

    fireEvent.click(screen.getByRole('button', { name: 'Contacted' }));

    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent(
        'Failed to mark contact. Could not reload contacts: Failed to fetch.'
      );
    });
    expect(screen.getByRole('alert').textContent).not.toMatch(/\.\./);
  });

  // --- #7723 follow-ups ---

  it('queues a retry PATCH behind an already in-flight Contacted action for the same row', async () => {
    const api = mockOutreachApi(
      [{ id: 1, name: 'Alice', contacted_at: null, created_at: '2026-09-26T09:00:00+00:00' }],
      { patchFailure: (callIndex) => callIndex === 0, deferContacted: true }
    );
    render(<OutreachPanel />);
    await screen.findByText('Alice');

    fireEvent.click(screen.getByRole('button', { name: 'Alice' }));
    const editInput = screen.getByLabelText('Edit name for Alice') as HTMLInputElement;
    fireEvent.change(editInput, { target: { value: 'Alicia' } });
    fireEvent.blur(editInput);

    await waitFor(() => {
      expect(api.patchCalls).toHaveLength(1);
    });
    const row = rowFor('Alicia');
    const retryButton = within(row).getByRole('button', { name: "Couldn't save — retry" });

    // Queue a Contacted action for the same row; the queue starts running it
    // right away (the response stays deferred).
    fireEvent.click(screen.getByRole('button', { name: 'Contacted' }));
    await waitFor(() => {
      expect(api.contactedCalls).toHaveLength(1);
    });

    // Click retry while Contacted is still in flight. The retry's PATCH must
    // be appended to the FIFO queue behind the already-running Contacted
    // action, not race ahead of it.
    fireEvent.click(retryButton);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(api.patchCalls).toHaveLength(1); // retry has not been sent yet

    api.resolve('contacted');
    await waitFor(() => {
      expect(api.patchCalls).toHaveLength(2);
    });
    expect(api.patchCalls[1]).toEqual({ id: 1, name: 'Alicia' });
    await waitFor(() => {
      expect(within(contactedList()).getByText('Saved')).toBeInTheDocument();
    });
  });

  it('drops the unsaved rename and removes the row once the resync reflects the delete, when a rename fails with 404', async () => {
    const user = userEvent.setup();
    const api = mockOutreachApi(
      [{ id: 1, name: 'Alice', contacted_at: null, created_at: '2026-09-26T09:00:00+00:00' }],
      // The row is deleted by another client at the moment this PATCH is
      // processed (mutating the mock's own store), and the follow-up resync
      // GET is held back under our control so we can observe both the
      // immediate dropField effect and, separately, the later removal.
      { patchDeletedElsewhere: true, deferGetAfterFirst: true }
    );
    render(<OutreachPanel />);
    await screen.findByText('Alice');

    await user.click(screen.getByRole('button', { name: 'Alice' }));
    const editInput = screen.getByLabelText('Edit name for Alice') as HTMLInputElement;
    await user.clear(editInput);
    await user.type(editInput, 'Alicia');
    await user.tab();

    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent('Contact not found');
    });
    // dropField happens synchronously in the failure handler, before the
    // still-pending resync GET returns: the unsaved value is already gone
    // and there is no retry control, even though the row itself (from the
    // stale pre-resync contacts list) is still on screen.
    expect(screen.getByRole('button', { name: 'Alice' })).toBeInTheDocument();
    expect(screen.queryByText('Alicia')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: "Couldn't save — retry" })).not.toBeInTheDocument();

    // Release the resync GET. It now reflects the server's real state (the
    // row is deleted), so the row disappears entirely.
    api.resolve('get');
    await waitFor(() => {
      expect(screen.queryByText('Alice')).not.toBeInTheDocument();
    });
  });

  it('drops the unsaved rename and removes the row once the resync reflects the reply, when a rename fails with 409', async () => {
    const user = userEvent.setup();
    const api = mockOutreachApi(
      [{ id: 1, name: 'Alice', contacted_at: null, created_at: '2026-09-26T09:00:00+00:00' }],
      // The row is replied to (from another tab) at the moment this PATCH is
      // processed, mutating the mock's own store the same way.
      { patchRepliedElsewhere: true, deferGetAfterFirst: true }
    );
    render(<OutreachPanel />);
    await screen.findByText('Alice');

    await user.click(screen.getByRole('button', { name: 'Alice' }));
    const editInput = screen.getByLabelText('Edit name for Alice') as HTMLInputElement;
    await user.clear(editInput);
    await user.type(editInput, 'Alicia');
    await user.tab();

    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent('Invalid name or contact state');
    });
    expect(screen.getByRole('button', { name: 'Alice' })).toBeInTheDocument();
    expect(screen.queryByText('Alicia')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: "Couldn't save — retry" })).not.toBeInTheDocument();

    api.resolve('get');
    await waitFor(() => {
      expect(screen.queryByText('Alice')).not.toBeInTheDocument();
    });
  });
});
