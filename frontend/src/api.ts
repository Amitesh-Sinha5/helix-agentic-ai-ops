/**
 * Typed API client.
 *
 * Two things it handles that callers should never have to think about:
 *
 *  - **Token attachment**: the access token is added to every request.
 *  - **Refresh on 401**: a single in-flight refresh is shared by all callers, so
 *    ten parallel requests that expire together trigger one refresh, not ten.
 *    Each request is then replayed once with the new token.
 */

import type {
  AuthResponse,
  CheckoutResponse,
  CodeReviewResult,
  DocumentSummary,
  IngestResponse,
  ObservabilitySummary,
  Page,
  QueryResponse,
  Subscription,
  TokenPair,
  TriageResponse,
  UsageResponse,
  User,
} from './types';

export const API_BASE_URL: string =
  (import.meta.env?.VITE_API_URL as string | undefined) ?? 'http://localhost:8000';

const ACCESS_KEY = 'helix.access_token';
const REFRESH_KEY = 'helix.refresh_token';
const USER_KEY = 'helix.user';

export class ApiError extends Error {
  // Declared explicitly rather than as constructor parameter properties, which
  // `erasableSyntaxOnly` (on by default in this template) disallows.
  readonly status: number;
  readonly code?: string;
  readonly body?: unknown;

  constructor(message: string, status: number, code?: string, body?: unknown) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.body = body;
  }

  get isRateLimited(): boolean {
    return this.status === 429;
  }
}

// --------------------------------------------------------------------------
// Token storage
// --------------------------------------------------------------------------
export const tokenStore = {
  get access(): string | null {
    return localStorage.getItem(ACCESS_KEY);
  },
  get refresh(): string | null {
    return localStorage.getItem(REFRESH_KEY);
  },
  get user(): User | null {
    const raw = localStorage.getItem(USER_KEY);
    if (!raw) return null;
    try {
      return JSON.parse(raw) as User;
    } catch {
      return null;
    }
  },
  save(tokens: TokenPair, user?: User) {
    localStorage.setItem(ACCESS_KEY, tokens.access_token);
    localStorage.setItem(REFRESH_KEY, tokens.refresh_token);
    if (user) localStorage.setItem(USER_KEY, JSON.stringify(user));
  },
  saveUser(user: User) {
    localStorage.setItem(USER_KEY, JSON.stringify(user));
  },
  clear() {
    [ACCESS_KEY, REFRESH_KEY, USER_KEY].forEach((k) => localStorage.removeItem(k));
  },
};

type Listener = () => void;
const logoutListeners = new Set<Listener>();

/** Notifies the app when the session dies so it can route back to login. */
export function onSessionExpired(listener: Listener): () => void {
  logoutListeners.add(listener);
  return () => logoutListeners.delete(listener);
}

function endSession() {
  tokenStore.clear();
  logoutListeners.forEach((listener) => listener());
}

// --------------------------------------------------------------------------
// Core request
// --------------------------------------------------------------------------
interface RequestOptions extends Omit<RequestInit, 'body'> {
  body?: unknown;
  auth?: boolean;
  retryOn401?: boolean;
}

/** Shared across concurrent callers so a burst of 401s causes one refresh. */
let refreshInFlight: Promise<boolean> | null = null;

async function refreshAccessToken(): Promise<boolean> {
  const refreshToken = tokenStore.refresh;
  if (!refreshToken) return false;

  if (!refreshInFlight) {
    refreshInFlight = (async () => {
      try {
        const response = await fetch(`${API_BASE_URL}/auth/refresh`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ refresh_token: refreshToken }),
        });
        if (!response.ok) return false;
        tokenStore.save((await response.json()) as TokenPair);
        return true;
      } catch {
        return false;
      } finally {
        // Cleared in a microtask so everyone awaiting this attempt observes the
        // same result before a new one can start.
        queueMicrotask(() => {
          refreshInFlight = null;
        });
      }
    })();
  }
  return refreshInFlight;
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { body, auth = true, retryOn401 = true, headers, ...rest } = options;

  const requestHeaders: Record<string, string> = {
    Accept: 'application/json',
    ...((headers as Record<string, string>) ?? {}),
  };
  if (body !== undefined && !(body instanceof FormData)) {
    requestHeaders['Content-Type'] = 'application/json';
  }
  if (auth && tokenStore.access) {
    requestHeaders.Authorization = `Bearer ${tokenStore.access}`;
  }

  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...rest,
    headers: requestHeaders,
    body:
      body === undefined ? undefined : body instanceof FormData ? body : JSON.stringify(body),
  });

  if (response.status === 401 && auth && retryOn401) {
    if (await refreshAccessToken()) {
      return request<T>(path, { ...options, retryOn401: false });
    }
    endSession();
  }

  if (!response.ok) {
    let payload: unknown = null;
    let message = `${response.status} ${response.statusText}`;
    try {
      payload = await response.json();
      const detail = (payload as { detail?: unknown })?.detail;
      if (typeof detail === 'string') message = detail;
      else if (detail) message = JSON.stringify(detail);
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(message, response.status, (payload as { code?: string })?.code, payload);
  }

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

// --------------------------------------------------------------------------
// Endpoints
// --------------------------------------------------------------------------
export const api = {
  // -- auth ---------------------------------------------------------------
  async signup(email: string, password: string, fullName?: string): Promise<AuthResponse> {
    const result = await request<AuthResponse>('/auth/signup', {
      method: 'POST',
      auth: false,
      body: { email, password, full_name: fullName || null },
    });
    tokenStore.save(result.tokens, result.user);
    return result;
  },

  async login(email: string, password: string): Promise<AuthResponse> {
    const result = await request<AuthResponse>('/auth/login', {
      method: 'POST',
      auth: false,
      body: { email, password },
    });
    tokenStore.save(result.tokens, result.user);
    return result;
  },

  async logout(): Promise<void> {
    const refreshToken = tokenStore.refresh;
    try {
      await request('/auth/logout', {
        method: 'POST',
        body: { refresh_token: refreshToken, all_sessions: false },
        retryOn401: false,
      });
    } catch {
      // A failed server-side revoke must not trap the user in a logged-in UI.
    }
    endSession();
  },

  me: () => request<User>('/auth/me'),

  // -- doc q&a ------------------------------------------------------------
  ingest: (payload: {
    title: string;
    text: string;
    source?: string;
    collection?: string;
  }) => request<IngestResponse>('/docs/ingest', { method: 'POST', body: payload }),

  ingestFile: (file: File, collection = 'documents') => {
    const form = new FormData();
    form.append('file', file);
    form.append('collection', collection);
    return request<IngestResponse>('/docs/ingest/file', { method: 'POST', body: form });
  },

  query: (
    payload: {
      question: string;
      collection?: string;
      session_id?: string | null;
      use_cache?: boolean;
      self_critique?: boolean;
    },
    requestId?: string,
  ) =>
    request<QueryResponse>('/docs/query', {
      method: 'POST',
      body: payload,
      // Passing our own id lets the trace socket subscribe *before* the
      // request is sent, so no early agent steps are missed.
      headers: requestId ? { 'X-Request-ID': requestId } : undefined,
    }),

  documents: () => request<Page<DocumentSummary>>('/docs/documents'),

  deleteDocument: (id: string) =>
    request<{ deleted: string; chunks_removed: number }>(`/docs/documents/${id}`, {
      method: 'DELETE',
    }),

  feedback: (payload: {
    request_id: string;
    rating: number;
    question?: string;
    answer?: string;
    comment?: string;
  }) => request<{ id: string; rating: number }>('/docs/feedback', { method: 'POST', body: payload }),

  // -- code review --------------------------------------------------------
  analyzeCode: (
    payload: { code: string; language?: string; filename?: string },
    requestId?: string,
  ) =>
    request<CodeReviewResult>('/code-review/analyze', {
      method: 'POST',
      body: payload,
      headers: requestId ? { 'X-Request-ID': requestId } : undefined,
    }),

  // -- support triage -----------------------------------------------------
  triage: (
    payload: { subject: string; body: string; customer_email?: string | null },
    requestId?: string,
  ) =>
    request<TriageResponse>('/support/triage', {
      method: 'POST',
      body: payload,
      headers: requestId ? { 'X-Request-ID': requestId } : undefined,
    }),

  // -- observability ------------------------------------------------------
  observability: (windowHours = 24) =>
    request<ObservabilitySummary>(`/observability/summary?window_hours=${windowHours}`),

  // -- billing ------------------------------------------------------------
  subscription: () => request<Subscription>('/billing/subscription'),
  usage: () => request<UsageResponse>('/billing/usage'),
  checkout: () =>
    request<CheckoutResponse>('/billing/checkout', { method: 'POST', body: { tier: 'pro' } }),
  completeSimulatedCheckout: (sessionId: string) =>
    request<Subscription>(`/billing/simulate-completion?session_id=${encodeURIComponent(sessionId)}`, {
      method: 'POST',
    }),

  health: () => request<{ status: string }>('/health', { auth: false }),
};

// --------------------------------------------------------------------------
// WebSockets
// --------------------------------------------------------------------------
export function wsUrl(path: string): string {
  const base = API_BASE_URL.replace(/^http/, 'ws');
  const token = tokenStore.access ?? '';
  return `${base}${path}?token=${encodeURIComponent(token)}`;
}

export function newRequestId(): string {
  const bytes = new Uint8Array(8);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
}
