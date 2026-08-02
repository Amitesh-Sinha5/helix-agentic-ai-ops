import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';

import { ApiError, api, newRequestId } from '../api';
import { AgentTrace } from '../components/AgentTrace';
import { useAgentTrace } from '../hooks/useAgentTrace';
import type { DocumentSummary, QueryResponse } from '../types';

const SAMPLE_DOC = `Helix Subscription and Refund Policy

Trial period. Every new Helix workspace begins with a free trial that lasts 14 days from the date of signup. No credit card is required to start the trial.

Refunds. Customers may request a full refund within 30 days of their first payment. Approved refunds are issued back to the original payment method within 5 to 10 business days.

Plan limits. The Free plan allows 20 agent requests per day per user. The Pro plan removes the daily request cap entirely and adds priority support.

Data retention. Documents uploaded to Helix are retained for 90 days after deletion in cold storage before being permanently destroyed.`;

export function DocQA() {
  const [question, setQuestion] = useState('');
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [result, setResult] = useState<QueryResponse | null>(null);
  const [documents, setDocuments] = useState<DocumentSummary[]>([]);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [feedbackSent, setFeedbackSent] = useState<number | null>(null);
  const [ingesting, setIngesting] = useState(false);

  const { events, status, connect, disconnect, merge, reset } = useAgentTrace();
  const fileInputRef = useRef<HTMLInputElement>(null);

  const [docsToken, setDocsToken] = useState(0);
  const reloadDocuments = useCallback(() => setDocsToken((n) => n + 1), []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const page = await api.documents();
        if (!cancelled) setDocuments(page.items);
      } catch {
        /* listing is non-critical */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [docsToken]);

  const ingest = async (payload: { title: string; text: string; source?: string }) => {
    setIngesting(true);
    setError(null);
    try {
      await api.ingest(payload);
      reloadDocuments();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Ingestion failed');
    } finally {
      setIngesting(false);
    }
  };

  const onUpload = async (file: File) => {
    setIngesting(true);
    setError(null);
    try {
      await api.ingestFile(file);
      reloadDocuments();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Upload failed');
    } finally {
      setIngesting(false);
      if (fileInputRef.current) fileInputRef.current.value = '';
    }
  };

  const onSubmit = async (event: FormEvent) => {
    event.preventDefault();
    if (!question.trim()) return;

    setRunning(true);
    setError(null);
    setResult(null);
    setFeedbackSent(null);
    reset();

    // Generate the id first and open the socket before sending the request, so
    // the trace streams from the very first node instead of arriving all at
    // once after the answer.
    const requestId = newRequestId();
    connect(requestId);

    try {
      const response = await api.query({ question, session_id: sessionId }, requestId);
      setResult(response);
      // Fold in the authoritative trace before the socket is closed below.
      merge(response.trace);
    } catch (err) {
      setError(
        err instanceof ApiError && err.isRateLimited
          ? `${err.message} — upgrade on the Billing page.`
          : err instanceof Error
            ? err.message
            : 'Query failed',
      );
    } finally {
      setRunning(false);
      disconnect();
    }
  };

  const sendFeedback = async (rating: number) => {
    if (!result) return;
    try {
      await api.feedback({
        request_id: result.request_id,
        rating,
        question: result.question,
        answer: result.answer,
      });
      setFeedbackSent(rating);
    } catch {
      /* feedback is best-effort */
    }
  };

  return (
    <div className="page">
      <div className="page-header">
        <div>
          <h1>Document Q&amp;A</h1>
          <p className="muted">
            Hybrid retrieval (vector + BM25 → reciprocal rank fusion → reranking) with an agentic
            re-query loop and a groundedness check.
          </p>
        </div>
      </div>

      <div className="grid grid-2">
        <section className="card">
          <h2>Knowledge base</h2>
          <p className="muted small">
            {documents.length
              ? `${documents.length} document(s), ${documents.reduce((n, d) => n + d.chunk_count, 0)} chunks indexed.`
              : 'No documents indexed yet.'}
          </p>

          <ul className="doc-list">
            {documents.map((doc) => (
              <li key={doc.id} className="doc-item">
                <span className="doc-title">{doc.title}</span>
                <span className="muted small">{doc.chunk_count} chunks</span>
                <button
                  type="button"
                  className="button button-ghost small"
                  onClick={async () => {
                    await api.deleteDocument(doc.id);
                    reloadDocuments();
                  }}
                >
                  Remove
                </button>
              </li>
            ))}
          </ul>

          <div className="row">
            <button
              type="button"
              className="button"
              disabled={ingesting}
              onClick={() =>
                ingest({ title: 'Subscription and Refund Policy', text: SAMPLE_DOC, source: 'policy.md' })
              }
            >
              {ingesting ? 'Indexing…' : 'Load sample policy'}
            </button>
            <input
              ref={fileInputRef}
              type="file"
              accept=".txt,.md,.pdf"
              aria-label="Upload a document"
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (file) void onUpload(file);
              }}
            />
          </div>
        </section>

        <section className="card">
          <h2>Ask a question</h2>
          <form onSubmit={onSubmit}>
            <label htmlFor="question">Question</label>
            <textarea
              id="question"
              rows={3}
              value={question}
              placeholder="How long does the free trial last?"
              onChange={(e) => setQuestion(e.target.value)}
            />

            <label className="checkbox">
              <input
                type="checkbox"
                checked={sessionId !== null}
                onChange={(e) => setSessionId(e.target.checked ? newRequestId() : null)}
              />
              Multi-turn session (enables follow-up questions)
            </label>

            <button type="submit" className="button button-primary" disabled={running || !question.trim()}>
              {running ? 'Running agents…' : 'Ask'}
            </button>
          </form>

          {error && (
            <p className="error" role="alert">
              {error}
            </p>
          )}
        </section>
      </div>

      <AgentTrace events={events} status={status} running={running} />

      {result && (
        <section className="card" data-testid="answer">
          <div className="answer-header">
            <h2>Answer</h2>
            <div className="answer-badges">
              {result.usage.cache_hit && <span className="badge badge-cache">cache hit</span>}
              {result.retrieval_loops > 1 && (
                <span className="badge badge-muted">{result.retrieval_loops} retrieval passes</span>
              )}
              {result.groundedness && (
                <span className={`badge ${result.groundedness.grounded ? 'badge-ok' : 'badge-warn'}`}>
                  groundedness {result.groundedness.score.toFixed(2)}
                </span>
              )}
            </div>
          </div>

          <p className={`answer-body ${result.found ? '' : 'is-not-found'}`}>{result.answer}</p>

          {result.reformulated_queries.length > 0 && (
            <p className="muted small">
              Reformulated query: <code>{result.reformulated_queries.at(-1)}</code>
            </p>
          )}

          {result.tool_invocations.length > 0 && (
            <div className="tool-results">
              <h3>Tool calls</h3>
              {result.tool_invocations.map((invocation) => (
                <pre key={invocation.tool} className="code-block small">
                  {invocation.tool}({JSON.stringify(invocation.arguments)}) →{' '}
                  {JSON.stringify(invocation.result, null, 2)}
                </pre>
              ))}
            </div>
          )}

          {result.citations.length > 0 && (
            <div className="citations">
              <h3>Sources</h3>
              <ol>
                {result.citations.map((citation) => (
                  <li key={citation.index}>
                    <strong>{citation.document_title ?? citation.source ?? 'document'}</strong>
                    <p className="muted small">{citation.snippet}</p>
                  </li>
                ))}
              </ol>
            </div>
          )}

          <div className="answer-footer">
            <span className="muted small">
              {result.usage.llm_calls} LLM calls · {result.usage.total_tokens} tokens · $
              {result.usage.cost_usd.toFixed(5)} · {result.usage.latency_ms.toFixed(0)}ms
            </span>
            <div className="row">
              <button
                type="button"
                className="button button-ghost small"
                disabled={feedbackSent !== null}
                onClick={() => sendFeedback(1)}
              >
                👍
              </button>
              <button
                type="button"
                className="button button-ghost small"
                disabled={feedbackSent !== null}
                onClick={() => sendFeedback(-1)}
              >
                👎
              </button>
              {feedbackSent !== null && <span className="muted small">Thanks for the feedback.</span>}
            </div>
          </div>
        </section>
      )}
    </div>
  );
}
