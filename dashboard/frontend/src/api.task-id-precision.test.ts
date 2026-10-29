// Snowflake-scale task ids must survive the full fetch -> PATCH round trip
// without losing precision (#7824).
//
// pt_next_id (#6044) generates 63-bit ids that routinely exceed
// Number.MAX_SAFE_INTEGER (2^53-1 == 9007199254740991). `Response.json()`
// calls the browser's native `JSON.parse` under the hood, which silently
// rounds any *unquoted* integer literal above that threshold --
// 98969975881101312 becomes 98969975881101310. Every PATCH built from that
// rounded id then hits the wrong row, or 404s outright.
//
// The fix is a backend contract change (dashboard/app.py emits every such
// id as a decimal string, never a bare number); these tests prove the
// frontend half of the contract: the exact string survives `fetchTasks()`
// and is exactly what `updateTask()` puts on the wire.

import { afterEach, describe, expect, it, vi } from 'vitest';

import { fetchTasks, updateTask } from './api';

// Real example from the bug report.
const BIG_ID = '98969975881101312';

afterEach(() => {
  vi.unstubAllGlobals();
});

function stubFetchOnce(rawJsonText: string): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn(() =>
    Promise.resolve({
      ok: true,
      // `JSON.parse` here is the real one -- exactly what `Response.json()`
      // does in a browser. Passing a pre-built object instead (as several
      // other test files in this repo do) would never exercise the bug:
      // the precision loss happens inside JSON.parse itself, on text that
      // contains a bare, oversized number literal.
      json: () => Promise.resolve(JSON.parse(rawJsonText)),
    } as Response)
  );
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

describe('task id precision (#7824)', () => {
  it('demonstrates the underlying bug: an unquoted id rounds under JSON.parse', () => {
    // This is exactly what dashboard/app.py emitted before #7824 -- the raw
    // task id as a bare JSON number. It documents why quoting is the fix;
    // it is not a test of this repo's code (JSON.parse is not ours), so it
    // is not expected to ever go red.
    const unquoted = `{"tasks":[{"id":${BIG_ID}}]}`;
    const parsed = JSON.parse(unquoted);
    expect(String(parsed.tasks[0].id)).not.toBe(BIG_ID);
  });

  it('fetchTasks returns the exact id when the server quotes it as a string', async () => {
    const fixedPayload = `{"tasks":[{"id":"${BIG_ID}","status":"Backlog"}],"total":1}`;
    stubFetchOnce(fixedPayload);

    const tasks = await fetchTasks();

    expect(tasks).toHaveLength(1);
    expect(tasks[0].id).toBe(BIG_ID);
    expect(typeof tasks[0].id).toBe('string');
  });

  it('updateTask PATCHes the exact-digit id, never a rounded one', async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ id: BIG_ID, status: 'Done' }),
      } as Response)
    );
    vi.stubGlobal('fetch', fetchMock);

    await updateTask(BIG_ID, { status: 'Done' });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, options] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe(`/api/tasks/${BIG_ID}`);
    expect(options).toMatchObject({ method: 'PATCH' });
  });

  it('a full fetch-then-update round trip never touches a rounded id', async () => {
    const getPayload = `{"tasks":[{"id":"${BIG_ID}","status":"Backlog"}],"total":1}`;
    const getMock = vi.fn(() =>
      Promise.resolve({ ok: true, json: () => Promise.resolve(JSON.parse(getPayload)) } as Response)
    );
    vi.stubGlobal('fetch', getMock);

    const [task] = await fetchTasks();

    const patchMock = vi.fn(() =>
      Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ id: task.id, status: 'To Do' }),
      } as Response)
    );
    vi.stubGlobal('fetch', patchMock);

    await updateTask(task.id, { status: 'To Do' });

    const [url] = patchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe(`/api/tasks/${BIG_ID}`);
  });
});
