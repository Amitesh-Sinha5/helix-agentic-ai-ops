import { useState, type FormEvent } from 'react';

import { api, newRequestId } from '../api';
import { AgentTrace } from '../components/AgentTrace';
import { useAgentTrace } from '../hooks/useAgentTrace';
import type { TriageResponse } from '../types';

const SAMPLES = [
  {
    label: 'Billing (trained model path)',
    subject: 'Charged twice this month',
    body: 'I was charged twice for my subscription and need the duplicate payment refunded.',
  },
  {
    label: 'Outage (escalates)',
    subject: 'Everything is down',
    body: 'Production is down and completely unreachable for all of our users. This is a critical outage.',
  },
  {
    label: 'Ambiguous (LLM fallback)',
    subject: 'Hello',
    body: 'A quick question about the thing we discussed last week.',
  },
];

export function SupportTriage() {
  const [subject, setSubject] = useState(SAMPLES[0].subject);
  const [body, setBody] = useState(SAMPLES[0].body);
  const [email, setEmail] = useState('');
  const [result, setResult] = useState<TriageResponse | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const { events, status, connect, disconnect, merge, reset } = useAgentTrace();

  const onSubmit = async (event: FormEvent) => {
    event.preventDefault();
    if (!subject.trim() || !body.trim()) return;

    setRunning(true);
    setError(null);
    setResult(null);
    reset();

    const requestId = newRequestId();
    connect(requestId);
    try {
      const response = await api.triage(
        { subject, body, customer_email: email || null },
        requestId,
      );
      setResult(response);
      merge(response.trace);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Triage failed');
    } finally {
      setRunning(false);
      disconnect();
    }
  };

  return (
    <div className="page">
      <div className="page-header">
        <div>
          <h1>Support Triage</h1>
          <p className="muted">
            A trained scikit-learn classifier handles confident tickets with no LLM call; only
            ambiguous ones fall through to an LLM classifier.
          </p>
        </div>
      </div>

      <section className="card">
        <div className="row wrap">
          {SAMPLES.map((sample) => (
            <button
              key={sample.label}
              type="button"
              className="button button-ghost small"
              onClick={() => {
                setSubject(sample.subject);
                setBody(sample.body);
              }}
            >
              {sample.label}
            </button>
          ))}
        </div>

        <form onSubmit={onSubmit}>
          <label htmlFor="subject">Subject</label>
          <input id="subject" value={subject} onChange={(e) => setSubject(e.target.value)} />

          <label htmlFor="ticket-body">Message</label>
          <textarea
            id="ticket-body"
            rows={5}
            value={body}
            onChange={(e) => setBody(e.target.value)}
          />

          <label htmlFor="customer-email">Customer email (optional)</label>
          <input
            id="customer-email"
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />

          <button type="submit" className="button button-primary" disabled={running}>
            {running ? 'Triaging…' : 'Triage ticket'}
          </button>
        </form>

        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
      </section>

      <AgentTrace events={events} status={status} running={running} />

      {result && (
        <section className="card" data-testid="triage-result">
          <div className="answer-header">
            <h2>Triage result</h2>
            <div className="answer-badges">
              <span className={`badge badge-${result.priority}`}>{result.priority}</span>
              <span className="badge badge-muted">{result.category}</span>
              <span
                className={`badge ${
                  result.classification_path === 'trained_model' ? 'badge-ok' : 'badge-warn'
                }`}
                title={
                  result.classification_path === 'trained_model'
                    ? 'Classified by the trained model — no LLM call'
                    : 'Trained model was not confident; an LLM classified this ticket'
                }
              >
                {result.classification_path === 'trained_model' ? 'trained model' : 'LLM fallback'} ·{' '}
                {(result.confidence * 100).toFixed(0)}%
              </span>
              {result.escalate && <span className="badge badge-urgent">escalated</span>}
            </div>
          </div>

          {result.escalate && (
            <p className="callout callout-warn">
              <strong>Escalated:</strong> {result.escalation_reason}
              {result.suggested_owner && ` → ${result.suggested_owner}`}
            </p>
          )}

          <h3>Draft reply</h3>
          <pre className="draft-response">{result.draft_response}</pre>

          {result.kb_sources.length > 0 && (
            <div className="citations">
              <h3>Grounded in</h3>
              <ol>
                {result.kb_sources.map((source) => (
                  <li key={source.document_id + source.snippet.slice(0, 20)}>
                    <strong>{source.title ?? 'Knowledge base'}</strong>{' '}
                    <span className="muted small">({source.score.toFixed(3)})</span>
                    <p className="muted small">{source.snippet}</p>
                  </li>
                ))}
              </ol>
            </div>
          )}

          <p className="muted small">
            {result.usage.llm_calls} LLM calls · {result.usage.total_tokens} tokens · $
            {result.usage.cost_usd.toFixed(5)} · {result.usage.latency_ms.toFixed(0)}ms
          </p>
        </section>
      )}
    </div>
  );
}
