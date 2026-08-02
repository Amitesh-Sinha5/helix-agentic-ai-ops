import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError, api, newRequestId, request, tokenStore, wsUrl } from '../api';

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('token storage', () => {
  beforeEach(() => localStorage.clear());

  it('round-trips tokens and the user', () => {
    tokenStore.save(
      { access_token: 'a', refresh_token: 'r', token_type: 'bearer', expires_in: 1800 },
      {
        id: 'u1',
        email: 'x@y.com',
        full_name: null,
        role: 'user',
        is_active: true,
        created_at: '2024-01-01',
      },
    );
    expect(tokenStore.access).toBe('a');
    expect(tokenStore.user?.email).toBe('x@y.com');

    tokenStore.clear();
    expect(tokenStore.access).toBeNull();
    expect(tokenStore.user).toBeNull();
  });

  it('survives a corrupted user blob instead of throwing', () => {
    localStorage.setItem('helix.user', '{not json');
    expect(tokenStore.user).toBeNull();
  });
});

describe('request', () => {
  beforeEach(() => localStorage.clear());

  it('attaches the bearer token', async () => {
    tokenStore.save({ access_token: 'tok', refresh_token: 'r', token_type: 'bearer', expires_in: 1 });
    const stub = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) =>
      jsonResponse({ ok: true }),
    );
    vi.stubGlobal('fetch', stub);

    await request('/auth/me');

    const headers = stub.mock.calls[0]![1]!.headers as Record<string, string>;
    expect(headers.Authorization).toBe('Bearer tok');
  });

  it('omits the token when auth is disabled', async () => {
    tokenStore.save({ access_token: 'tok', refresh_token: 'r', token_type: 'bearer', expires_in: 1 });
    const stub = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) =>
      jsonResponse({ ok: true }),
    );
    vi.stubGlobal('fetch', stub);

    await request('/health', { auth: false });

    const headers = stub.mock.calls[0]![1]!.headers as Record<string, string>;
    expect(headers.Authorization).toBeUndefined();
  });

  it('refreshes once on 401 and replays the request', async () => {
    tokenStore.save({ access_token: 'stale', refresh_token: 'r', token_type: 'bearer', expires_in: 1 });

    const stub = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/auth/refresh')) {
        return jsonResponse({
          access_token: 'fresh',
          refresh_token: 'r2',
          token_type: 'bearer',
          expires_in: 1800,
        });
      }
      return tokenStore.access === 'fresh'
        ? jsonResponse({ id: 'u1' })
        : jsonResponse({ detail: 'expired' }, 401);
    });
    vi.stubGlobal('fetch', stub);

    await expect(request<{ id: string }>('/auth/me')).resolves.toEqual({ id: 'u1' });
    expect(tokenStore.access).toBe('fresh');
    // original 401, refresh, replay
    expect(stub).toHaveBeenCalledTimes(3);
  });

  it('shares one refresh across concurrent 401s', async () => {
    tokenStore.save({ access_token: 'stale', refresh_token: 'r', token_type: 'bearer', expires_in: 1 });
    let refreshCalls = 0;

    const stub = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/auth/refresh')) {
        refreshCalls += 1;
        return jsonResponse({
          access_token: 'fresh',
          refresh_token: 'r2',
          token_type: 'bearer',
          expires_in: 1800,
        });
      }
      return tokenStore.access === 'fresh'
        ? jsonResponse({ ok: true })
        : jsonResponse({ detail: 'expired' }, 401);
    });
    vi.stubGlobal('fetch', stub);

    await Promise.all([request('/a'), request('/b'), request('/c')]);
    expect(refreshCalls).toBe(1);
  });

  it('clears the session when the refresh itself fails', async () => {
    tokenStore.save({ access_token: 'stale', refresh_token: 'bad', token_type: 'bearer', expires_in: 1 });
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => jsonResponse({ detail: 'nope' }, 401)),
    );

    await expect(request('/auth/me')).rejects.toBeInstanceOf(ApiError);
    expect(tokenStore.access).toBeNull();
  });

  it('surfaces the server error detail', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => jsonResponse({ detail: 'Rate limit exceeded', code: 'rate_limit_exceeded' }, 429)),
    );

    await expect(request('/docs/query', { auth: false })).rejects.toMatchObject({
      status: 429,
      message: 'Rate limit exceeded',
      code: 'rate_limit_exceeded',
    });
  });

  it('flags rate-limit errors', () => {
    expect(new ApiError('x', 429).isRateLimited).toBe(true);
    expect(new ApiError('x', 500).isRateLimited).toBe(false);
  });

  it('does not force a JSON content type on FormData uploads', async () => {
    const stub = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) =>
      jsonResponse({ ok: true }),
    );
    vi.stubGlobal('fetch', stub);

    const form = new FormData();
    form.append('file', new Blob(['hi']), 'a.txt');
    await request('/docs/ingest/file', { method: 'POST', body: form, auth: false });

    const headers = stub.mock.calls[0]![1]!.headers as Record<string, string>;
    // The browser must set the multipart boundary itself.
    expect(headers['Content-Type']).toBeUndefined();
  });
});

describe('api surface', () => {
  beforeEach(() => localStorage.clear());

  it('stores tokens after login', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        jsonResponse({
          user: { id: 'u1', email: 'a@b.com', full_name: null, role: 'user', is_active: true, created_at: 'x' },
          tokens: { access_token: 'A', refresh_token: 'R', token_type: 'bearer', expires_in: 1800 },
        }),
      ),
    );

    const result = await api.login('a@b.com', 'passw0rd1');
    expect(result.user.email).toBe('a@b.com');
    expect(tokenStore.access).toBe('A');
  });

  it('clears the session even when the logout call fails', async () => {
    tokenStore.save({ access_token: 'A', refresh_token: 'R', token_type: 'bearer', expires_in: 1 });
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        throw new Error('network down');
      }),
    );

    await api.logout();
    expect(tokenStore.access).toBeNull();
  });

  it('forwards the request id so the trace socket can subscribe first', async () => {
    const stub = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) =>
      jsonResponse({ answer: 'x' }),
    );
    vi.stubGlobal('fetch', stub);

    await api.query({ question: 'hello there' }, 'req-123');

    const headers = stub.mock.calls[0]![1]!.headers as Record<string, string>;
    expect(headers['X-Request-ID']).toBe('req-123');
  });
});

describe('websocket helpers', () => {
  it('converts the base url and carries the token', () => {
    tokenStore.save({ access_token: 'tok en', refresh_token: 'r', token_type: 'bearer', expires_in: 1 });
    const url = wsUrl('/ws/agent-status/abc');
    expect(url).toMatch(/^ws:\/\//);
    expect(url).toContain('/ws/agent-status/abc');
    expect(url).toContain('token=tok%20en');
  });

  it('generates distinct request ids', () => {
    const ids = new Set(Array.from({ length: 50 }, () => newRequestId()));
    expect(ids.size).toBe(50);
    expect([...ids][0]).toMatch(/^[0-9a-f]{16}$/);
  });
});
