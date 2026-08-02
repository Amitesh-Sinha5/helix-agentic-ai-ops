import { render, type RenderOptions } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import type { ReactElement } from 'react';
import { vi } from 'vitest';

import { AuthProvider } from '../auth/AuthContext';
import type { User } from '../types';

export const adminUser: User = {
  id: 'u-admin',
  email: 'admin@helix.example.com',
  full_name: 'Admin',
  role: 'admin',
  is_active: true,
  created_at: new Date().toISOString(),
};

export const regularUser: User = { ...adminUser, id: 'u-1', email: 'user@helix.example.com', role: 'user' };

/** Put a logged-in session in localStorage before rendering. */
export function seedSession(user: User = regularUser) {
  localStorage.setItem('helix.access_token', 'test-access-token');
  localStorage.setItem('helix.refresh_token', 'test-refresh-token');
  localStorage.setItem('helix.user', JSON.stringify(user));
}

export function renderWithProviders(
  ui: ReactElement,
  { route = '/', ...options }: RenderOptions & { route?: string } = {},
) {
  return render(
    <MemoryRouter initialEntries={[route]}>
      <AuthProvider>{ui}</AuthProvider>
    </MemoryRouter>,
    options,
  );
}

/** Renders without AuthProvider, for components that take props directly. */
export function renderBare(ui: ReactElement, { route = '/' }: { route?: string } = {}) {
  return render(<MemoryRouter initialEntries={[route]}>{ui}</MemoryRouter>);
}

interface FetchRoute {
  match: (url: string, init?: RequestInit) => boolean;
  respond: (url: string, init?: RequestInit) => { status?: number; body: unknown };
}

/**
 * Installs a fetch stub driven by a small routing table.
 *
 * Route-based rather than call-ordered, because components legitimately fire
 * requests concurrently and an order-sensitive mock makes those tests flaky.
 */
export function mockFetch(routes: FetchRoute[]) {
  const calls: Array<{ url: string; init?: RequestInit }> = [];

  const stub = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    calls.push({ url, init });
    const route = routes.find((r) => r.match(url, init));
    if (!route) {
      return new Response(JSON.stringify({ detail: `unmocked: ${url}` }), {
        status: 404,
        headers: { 'Content-Type': 'application/json' },
      });
    }
    const { status = 200, body } = route.respond(url, init);
    return new Response(JSON.stringify(body), {
      status,
      headers: { 'Content-Type': 'application/json' },
    });
  });

  vi.stubGlobal('fetch', stub);
  return { stub, calls };
}

export function route(
  pattern: string,
  body: unknown,
  { status = 200, method }: { status?: number; method?: string } = {},
): FetchRoute {
  return {
    match: (url, init) =>
      url.includes(pattern) && (!method || (init?.method ?? 'GET').toUpperCase() === method),
    respond: () => ({ status, body }),
  };
}
