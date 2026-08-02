import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it } from 'vitest';

import App from '../App';
import { AgentTrace } from '../components/AgentTrace';
import { Billing } from '../pages/Billing';
import { CodeReview } from '../pages/CodeReview';
import { DocQA } from '../pages/DocQA';
import { Login } from '../pages/Login';
import { Signup } from '../pages/Signup';
import { SupportTriage } from '../pages/SupportTriage';
import type { TraceEvent } from '../types';
import { stubClipboard } from './setup';
import {
  adminUser,
  mockFetch,
  regularUser,
  renderBare,
  renderWithProviders,
  route,
  seedSession,
} from './utils';

const authPayload = (user = regularUser) => ({
  user,
  tokens: { access_token: 'A', refresh_token: 'R', token_type: 'bearer', expires_in: 1800 },
});

const emptyDocs = { items: [], total: 0, limit: 50, offset: 0 };

beforeEach(() => localStorage.clear());

// --------------------------------------------------------------------------
// Auth pages
// --------------------------------------------------------------------------
describe('Login', () => {
  it('submits credentials and stores the session', async () => {
    const { calls } = mockFetch([route('/auth/login', authPayload())]);
    renderWithProviders(<Login />);

    await userEvent.type(screen.getByLabelText(/email/i), 'user@helix.example.com');
    await userEvent.type(screen.getByLabelText(/password/i), 'passw0rd1');
    await userEvent.click(screen.getByRole('button', { name: /sign in/i }));

    await waitFor(() => expect(localStorage.getItem('helix.access_token')).toBe('A'));
    const body = JSON.parse(calls.at(-1)!.init!.body as string);
    expect(body).toEqual({ email: 'user@helix.example.com', password: 'passw0rd1' });
  });

  it('shows the server error message on bad credentials', async () => {
    mockFetch([route('/auth/login', { detail: 'Incorrect email or password' }, { status: 401 })]);
    renderWithProviders(<Login />);

    await userEvent.type(screen.getByLabelText(/email/i), 'user@helix.example.com');
    await userEvent.type(screen.getByLabelText(/password/i), 'wrong');
    await userEvent.click(screen.getByRole('button', { name: /sign in/i }));

    expect(await screen.findByRole('alert')).toHaveTextContent('Incorrect email or password');
    expect(localStorage.getItem('helix.access_token')).toBeNull();
  });
});

describe('Signup', () => {
  it('creates an account and stores the session', async () => {
    const { calls } = mockFetch([route('/auth/signup', authPayload(adminUser))]);
    renderWithProviders(<Signup />);

    await userEvent.type(screen.getByLabelText(/full name/i), 'Ada');
    await userEvent.type(screen.getByLabelText(/email/i), 'ada@helix.example.com');
    await userEvent.type(screen.getByLabelText(/password/i), 'passw0rd1');
    await userEvent.click(screen.getByRole('button', { name: /create account/i }));

    await waitFor(() => expect(localStorage.getItem('helix.access_token')).toBe('A'));
    expect(JSON.parse(calls.at(-1)!.init!.body as string).full_name).toBe('Ada');
  });
});

// --------------------------------------------------------------------------
// Doc Q&A
// --------------------------------------------------------------------------
describe('DocQA', () => {
  const queryResponse = {
    request_id: 'r1',
    question: 'How long is the free trial?',
    answer: 'The free trial lasts 14 days from the date of signup.',
    found: true,
    citations: [
      {
        index: 1,
        document_id: 'd1',
        document_title: 'Policy',
        source: 'policy.md',
        snippet: 'The free trial lasts 14 days.',
      },
    ],
    chunks: [],
    retrieval_loops: 2,
    reformulated_queries: ['free trial length signup days'],
    groundedness: { grounded: true, score: 0.95, reason: 'ok', answer_relevance: 0.8 },
    critique: null,
    tool_invocations: [],
    trace: [],
    usage: {
      request_id: 'r1',
      latency_ms: 120,
      llm_calls: 5,
      total_tokens: 900,
      cost_usd: 0.0031,
      cache_hit: false,
    },
  };

  it('renders the answer with citations and cost after a query', async () => {
    seedSession();
    mockFetch([
      route('/auth/me', regularUser),
      route('/docs/documents', emptyDocs),
      route('/docs/query', queryResponse),
    ]);
    renderWithProviders(<DocQA />);

    await userEvent.type(screen.getByLabelText('Question'), 'How long is the free trial?');
    await userEvent.click(screen.getByRole('button', { name: /^ask$/i }));

    const answer = await screen.findByTestId('answer');
    // The citation snippet also contains "14 days"; assert on the answer body.
    expect(answer.querySelector('.answer-body')).toHaveTextContent(
      'The free trial lasts 14 days from the date of signup.',
    );
    expect(within(answer).getByText('Policy')).toBeInTheDocument();
    expect(within(answer).getByText(/2 retrieval passes/)).toBeInTheDocument();
    expect(within(answer).getByText(/groundedness 0\.95/)).toBeInTheDocument();
    expect(within(answer).getByText(/5 LLM calls/)).toBeInTheDocument();
  });

  it('opens a trace socket before firing the query', async () => {
    seedSession();
    mockFetch([
      route('/auth/me', regularUser),
      route('/docs/documents', emptyDocs),
      route('/docs/query', queryResponse),
    ]);
    renderWithProviders(<DocQA />);

    await userEvent.type(screen.getByLabelText('Question'), 'How long is the free trial?');
    await userEvent.click(screen.getByRole('button', { name: /^ask$/i }));

    const { MockWebSocket } = await import('./setup');
    await waitFor(() => expect(MockWebSocket.instances.length).toBeGreaterThan(0));
    expect(MockWebSocket.instances[0].url).toContain('/ws/agent-status/');
  });

  it('renders an abstention distinctly and without citations', async () => {
    seedSession();
    mockFetch([
      route('/auth/me', regularUser),
      route('/docs/documents', emptyDocs),
      route('/docs/query', {
        ...queryResponse,
        answer: 'I could not find that in the provided documents.',
        found: false,
        citations: [],
      }),
    ]);
    renderWithProviders(<DocQA />);

    await userEvent.type(screen.getByLabelText('Question'), 'Unrelated question entirely?');
    await userEvent.click(screen.getByRole('button', { name: /^ask$/i }));

    const answer = await screen.findByTestId('answer');
    expect(within(answer).getByText(/could not find/i)).toBeInTheDocument();
    expect(within(answer).queryByText('Sources')).not.toBeInTheDocument();
  });

  it('points a rate-limited user at the billing page', async () => {
    seedSession();
    mockFetch([
      route('/auth/me', regularUser),
      route('/docs/documents', emptyDocs),
      route(
        '/docs/query',
        { detail: 'Rate limit exceeded: 20/20 requests used', code: 'rate_limit_exceeded' },
        { status: 429 },
      ),
    ]);
    renderWithProviders(<DocQA />);

    await userEvent.type(screen.getByLabelText('Question'), 'Another question here');
    await userEvent.click(screen.getByRole('button', { name: /^ask$/i }));

    expect(await screen.findByRole('alert')).toHaveTextContent(/upgrade on the Billing page/i);
  });

  it('lists indexed documents', async () => {
    seedSession();
    mockFetch([
      route('/auth/me', regularUser),
      route('/docs/documents', {
        items: [
          {
            id: 'd1',
            title: 'Refund Policy',
            source: 'policy.md',
            collection: 'documents',
            chunk_count: 4,
            char_count: 900,
            status: 'ready',
            created_at: '2024-01-01',
          },
        ],
        total: 1,
        limit: 50,
        offset: 0,
      }),
    ]);
    renderWithProviders(<DocQA />);

    expect(await screen.findByText('Refund Policy')).toBeInTheDocument();
    expect(screen.getByText('1 document(s), 4 chunks indexed.')).toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------
// Code review
// --------------------------------------------------------------------------
describe('CodeReview', () => {
  it('renders findings grouped by severity with the verdict', async () => {
    seedSession();
    mockFetch([
      route('/auth/me', regularUser),
      route('/code-review/analyze', {
        request_id: 'r2',
        review_id: 'rev1',
        filename: 'reports.py',
        language: 'python',
        verdict: 'request_changes',
        summary: 'Blocking issues found.',
        issues: [
          {
            severity: 'critical',
            category: 'security',
            line: 6,
            title: 'Possible SQL injection',
            explanation: 'Query built with string formatting.',
            suggestion: 'Use parameterised queries.',
            agent: 'security',
          },
          {
            severity: 'high',
            category: 'correctness',
            line: 5,
            title: 'Mutable default argument',
            explanation: 'Shared across calls.',
            suggestion: 'Default to None.',
            agent: 'quality',
          },
        ],
        issue_count: 2,
        blocking_count: 2,
        severity_counts: { critical: 1, high: 1 },
        top_recommendation: 'Use parameterised queries.',
        trace: [],
        usage: {
          request_id: 'r2',
          latency_ms: 90,
          llm_calls: 3,
          total_tokens: 500,
          cost_usd: 0.002,
          cache_hit: false,
        },
      }),
    ]);
    renderWithProviders(<CodeReview />);

    await userEvent.click(screen.getByRole('button', { name: /review code/i }));

    const result = await screen.findByTestId('review-result');
    expect(within(result).getByText('Request changes')).toBeInTheDocument();
    expect(within(result).getAllByTestId('issue')).toHaveLength(2);
    expect(within(result).getByText('Possible SQL injection')).toBeInTheDocument();
    expect(within(result).getByText('security')).toBeInTheDocument();
    expect(within(result).getByText(/line 6/)).toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------
// Support triage
// --------------------------------------------------------------------------
describe('SupportTriage', () => {
  const triageBody = (overrides: Record<string, unknown> = {}) => ({
    request_id: 'r3',
    ticket_id: 't1',
    subject: 'Charged twice',
    priority: 'high',
    category: 'billing',
    confidence: 0.82,
    classification_path: 'trained_model',
    draft_response: 'Hi there, we will refund the duplicate charge.',
    escalate: false,
    escalation_reason: null,
    suggested_owner: null,
    kb_sources: [],
    trace: [],
    usage: {
      request_id: 'r3',
      latency_ms: 60,
      llm_calls: 2,
      total_tokens: 300,
      cost_usd: 0.001,
      cache_hit: false,
    },
    ...overrides,
  });

  it('shows the trained-model path when the classifier was confident', async () => {
    seedSession();
    mockFetch([route('/auth/me', regularUser), route('/support/triage', triageBody())]);
    renderWithProviders(<SupportTriage />);

    await userEvent.click(screen.getByRole('button', { name: /triage ticket/i }));

    const result = await screen.findByTestId('triage-result');
    expect(within(result).getByText(/trained model · 82%/)).toBeInTheDocument();
    expect(within(result).getByText('billing')).toBeInTheDocument();
    expect(within(result).queryByText('escalated')).not.toBeInTheDocument();
  });

  it('flags the LLM fallback path and an escalation', async () => {
    seedSession();
    mockFetch([
      route('/auth/me', regularUser),
      route(
        '/support/triage',
        triageBody({
          classification_path: 'llm_fallback',
          confidence: 0.41,
          priority: 'urgent',
          category: 'bug',
          escalate: true,
          escalation_reason: 'Total outage affecting all users.',
          suggested_owner: 'on-call-engineer',
        }),
      ),
    ]);
    renderWithProviders(<SupportTriage />);

    await userEvent.click(screen.getByRole('button', { name: /triage ticket/i }));

    const result = await screen.findByTestId('triage-result');
    expect(within(result).getByText(/LLM fallback · 41%/)).toBeInTheDocument();
    expect(within(result).getByText('escalated')).toBeInTheDocument();
    expect(within(result).getByText(/Total outage/)).toBeInTheDocument();
    expect(within(result).getByText(/on-call-engineer/)).toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------
// Billing
// --------------------------------------------------------------------------
describe('Billing', () => {
  it('shows the free plan with a usage bar and an upgrade path', async () => {
    seedSession();
    mockFetch([
      route('/auth/me', regularUser),
      route('/billing/subscription', {
        tier: 'free',
        status: 'active',
        current_period_end: null,
        cancel_at_period_end: false,
        stripe_customer_id: null,
      }),
      route('/billing/usage', {
        tier: 'free',
        used: 12,
        limit: 20,
        remaining: 8,
        window_seconds: 86400,
        resets_in_seconds: 3600,
        unlimited: false,
      }),
    ]);
    renderWithProviders(<Billing />);

    const plan = await screen.findByTestId('plan-card');
    expect(within(plan).getByText('FREE')).toBeInTheDocument();

    const usage = screen.getByTestId('usage-card');
    expect(within(usage).getByText('12')).toBeInTheDocument();
    expect(within(usage).getByRole('progressbar')).toHaveAttribute('aria-valuenow', '12');
    expect(screen.getAllByRole('button', { name: /upgrade to pro/i }).length).toBeGreaterThan(0);
  });

  it('shows unlimited usage and no upgrade button on Pro', async () => {
    seedSession();
    mockFetch([
      route('/auth/me', regularUser),
      route('/billing/subscription', {
        tier: 'pro',
        status: 'active',
        current_period_end: '2030-01-01T00:00:00Z',
        cancel_at_period_end: false,
        stripe_customer_id: 'cus_1',
      }),
      route('/billing/usage', {
        tier: 'pro',
        used: 340,
        limit: -1,
        remaining: -1,
        window_seconds: 86400,
        resets_in_seconds: 3600,
        unlimited: true,
      }),
    ]);
    renderWithProviders(<Billing />);

    const plan = await screen.findByTestId('plan-card');
    expect(within(plan).getByText('PRO')).toBeInTheDocument();
    expect(screen.getByText('∞', { exact: false })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /upgrade to pro/i })).not.toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------
// Trace rendering
// --------------------------------------------------------------------------
describe('AgentTrace', () => {
  const event = (over: Partial<TraceEvent>): TraceEvent => ({
    request_id: 'r',
    pod: 'doc_qa',
    node: 'retriever',
    phase: 'finish',
    sequence: 1,
    duration_ms: 14,
    detail: {},
    message: '',
    ...over,
  });

  it('pairs start and finish into one step and shows the duration', () => {
    renderBare(
      <AgentTrace
        events={[
          event({ phase: 'start', sequence: 1, message: 'searching' }),
          event({
            phase: 'finish',
            sequence: 2,
            message: 'kept 3 chunks',
            duration_ms: 21.4,
            detail: { vector_hits: 5, keyword_hits: 4, reranker: 'lexical' },
          }),
        ]}
        status="closed"
        running={false}
      />,
    );

    const steps = screen.getAllByTestId('trace-step');
    expect(steps).toHaveLength(1);
    expect(screen.getByText('Hybrid retriever')).toBeInTheDocument();
    expect(screen.getByText('kept 3 chunks')).toBeInTheDocument();
    expect(screen.getByText('21ms')).toBeInTheDocument();
  });

  it('expands a step to reveal the structured detail', async () => {
    renderBare(
      <AgentTrace
        events={[
          event({
            phase: 'finish',
            detail: { vector_hits: 5, keyword_hits: 4, reranker: 'cross-encoder' },
          }),
        ]}
        status="closed"
        running={false}
      />,
    );

    expect(screen.queryByText('cross-encoder')).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: /Hybrid retriever/i }));
    expect(screen.getByText('cross-encoder')).toBeInTheDocument();
    expect(screen.getByText('vector hits')).toBeInTheDocument();
  });

  it('keeps two retrieval passes as separate steps', () => {
    renderBare(
      <AgentTrace
        events={[
          event({ node: 'retriever', phase: 'start', sequence: 1 }),
          event({ node: 'retriever', phase: 'finish', sequence: 2, message: 'pass 1' }),
          event({ node: 'reformulate', phase: 'start', sequence: 3 }),
          event({ node: 'reformulate', phase: 'finish', sequence: 4, message: 'rewritten' }),
          event({ node: 'retriever', phase: 'start', sequence: 5 }),
          event({ node: 'retriever', phase: 'finish', sequence: 6, message: 'pass 2' }),
        ]}
        status="closed"
        running={false}
      />,
    );

    expect(screen.getAllByTestId('trace-step')).toHaveLength(3);
    expect(screen.getByText('pass 1')).toBeInTheDocument();
    expect(screen.getByText('pass 2')).toBeInTheDocument();
  });

  it('renders nothing when idle with no events', () => {
    const { container } = renderBare(<AgentTrace events={[]} status="idle" running={false} />);
    expect(container.querySelector('.trace')).toBeNull();
  });
});

// --------------------------------------------------------------------------
// Routing and guards
// --------------------------------------------------------------------------
describe('route guards', () => {
  it('redirects an anonymous visitor to the login page', async () => {
    mockFetch([]);
    renderWithProviders(<App />, { route: '/doc-qa' });
    expect(await screen.findByRole('heading', { name: /sign in to helix/i })).toBeInTheDocument();
  });

  it('keeps a non-admin off the observability page', async () => {
    seedSession(regularUser);
    mockFetch([
      route('/auth/me', regularUser),
      route('/docs/documents', emptyDocs),
    ]);
    renderWithProviders(<App />, { route: '/observability' });

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: /document q&a/i })).toBeInTheDocument(),
    );
    expect(screen.queryByRole('heading', { name: /^observability$/i })).not.toBeInTheDocument();
  });

  it('shows the observability nav link only to admins', async () => {
    seedSession(adminUser);
    mockFetch([
      route('/auth/me', adminUser),
      route('/docs/documents', emptyDocs),
    ]);
    renderWithProviders(<App />, { route: '/doc-qa' });

    expect(await screen.findByRole('link', { name: /observability/i })).toBeInTheDocument();
  });
});

describe('EscalationPanel', () => {
  it('appears for admins and renders a pushed escalation', async () => {
    seedSession(adminUser);
    mockFetch([route('/auth/me', adminUser), route('/docs/documents', emptyDocs)]);
    renderWithProviders(<App />, { route: '/doc-qa' });

    const panel = await screen.findByTestId('escalation-panel');
    const { MockWebSocket } = await import('./setup');
    const socket = MockWebSocket.instances.find((s) => s.url.includes('/ws/admin/escalations'));
    expect(socket).toBeDefined();

    socket!.open();
    socket!.emit({
      type: 'escalation',
      event: {
        ticket_id: 't9',
        request_id: 'r9',
        subject: 'Total outage',
        priority: 'urgent',
        category: 'bug',
        reason: 'All users affected',
        suggested_owner: 'on-call-engineer',
        customer_email: null,
        created_at: '2024-01-01T00:00:00Z',
      },
    });

    expect(await within(panel).findByText('Total outage')).toBeInTheDocument();
    expect(within(panel).getByText('All users affected')).toBeInTheDocument();
  });

  it('is absent for non-admins', async () => {
    seedSession(regularUser);
    mockFetch([route('/auth/me', regularUser), route('/docs/documents', emptyDocs)]);
    renderWithProviders(<App />, { route: '/doc-qa' });

    await screen.findByRole('heading', { name: /document q&a/i });
    expect(screen.queryByTestId('escalation-panel')).not.toBeInTheDocument();
  });
});

describe('CodeReview editor', () => {
  const emptyReview = {
    request_id: 'r9',
    review_id: 'rev9',
    filename: 'x.py',
    language: 'python',
    verdict: 'approve',
    summary: 'ok',
    issues: [],
    issue_count: 0,
    blocking_count: 0,
    severity_counts: {},
    top_recommendation: null,
    trace: [],
    usage: {
      request_id: 'r9',
      latency_ms: 1,
      llm_calls: 3,
      total_tokens: 10,
      cost_usd: 0.001,
      cache_hit: false,
    },
  };

  it('accepts pasted code, replacing the sample', async () => {
    seedSession();
    mockFetch([route('/auth/me', regularUser)]);
    renderWithProviders(<CodeReview />);

    const editor = screen.getByLabelText('Code') as HTMLTextAreaElement;
    expect(editor.value).toContain('API_KEY');

    // Clear, then type — the same sequence a paste performs.
    await userEvent.click(screen.getByRole('button', { name: /^clear$/i }));
    expect(editor.value).toBe('');

    await userEvent.type(editor, 'def hi():\n    return 1');
    expect(editor.value).toBe('def hi():\n    return 1');
    expect(screen.getByText(/2 lines/)).toBeInTheDocument();
  });

  it('replaces the editor contents from the clipboard', async () => {
    seedSession();
    mockFetch([route('/auth/me', regularUser)]);
    stubClipboard(async () => 'print("pasted")');
    renderWithProviders(<CodeReview />);

    await userEvent.click(screen.getByRole('button', { name: /paste from clipboard/i }));

    const editor = screen.getByLabelText('Code') as HTMLTextAreaElement;
    await waitFor(() => expect(editor.value).toBe('print("pasted")'));
    expect(editor.value).not.toContain('API_KEY');
  });

  it('tells the user to paste manually when the clipboard is blocked', async () => {
    seedSession();
    mockFetch([route('/auth/me', regularUser)]);
    stubClipboard(async () => {
      throw new Error('denied');
    });
    renderWithProviders(<CodeReview />);

    await userEvent.click(screen.getByRole('button', { name: /paste from clipboard/i }));
    expect(await screen.findByRole('alert')).toHaveTextContent(/Ctrl\+V/);
  });

  it('loads an uploaded file and infers the language from its extension', async () => {
    seedSession();
    mockFetch([route('/auth/me', regularUser), route('/code-review/analyze', emptyReview)]);
    renderWithProviders(<CodeReview />);

    const file = new File(['const a = 1;'], 'app.ts', { type: 'text/plain' });
    await userEvent.upload(screen.getByLabelText(/upload a source file/i), file);

    const editor = screen.getByLabelText('Code') as HTMLTextAreaElement;
    await waitFor(() => expect(editor.value).toBe('const a = 1;'));
    expect(screen.getByLabelText('Filename')).toHaveValue('app.ts');
    expect(screen.getByText(/detected typescript/)).toBeInTheDocument();
  });

  it('sends the detected language, not a hardcoded one', async () => {
    seedSession();
    const { calls } = mockFetch([
      route('/auth/me', regularUser),
      route('/code-review/analyze', emptyReview),
    ]);
    renderWithProviders(<CodeReview />);

    await userEvent.clear(screen.getByLabelText('Filename'));
    await userEvent.type(screen.getByLabelText('Filename'), 'main.go');
    await userEvent.click(screen.getByRole('button', { name: /review code/i }));

    await waitFor(() => {
      const analyze = calls.find((c) => c.url.includes('/code-review/analyze'));
      expect(analyze).toBeDefined();
      expect(JSON.parse(analyze!.init!.body as string).language).toBe('go');
    });
  });

  it('cannot submit an empty editor', async () => {
    seedSession();
    mockFetch([route('/auth/me', regularUser)]);
    renderWithProviders(<CodeReview />);

    await userEvent.click(screen.getByRole('button', { name: /^clear$/i }));
    expect(screen.getByRole('button', { name: /review code/i })).toBeDisabled();
  });
});
