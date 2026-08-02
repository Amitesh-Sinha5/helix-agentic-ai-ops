import { useCallback, useEffect, useState } from 'react';
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';

import { api } from '../api';
import type { ObservabilitySummary } from '../types';

const POD_COLORS: Record<string, string> = {
  doc_qa: '#5b8def',
  code_review: '#28a49c',
  support_triage: '#c46be0',
};

function Stat({
  label,
  value,
  hint,
}: {
  label: string;
  value: string;
  hint?: string;
}) {
  return (
    <div className="stat">
      <span className="stat-label">{label}</span>
      <span className="stat-value">{value}</span>
      {hint && <span className="stat-hint">{hint}</span>}
    </div>
  );
}

export function Observability() {
  const [summary, setSummary] = useState<ObservabilitySummary | null>(null);
  const [windowHours, setWindowHours] = useState(24);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const [reloadToken, setReloadToken] = useState(0);

  // The cancelled flag guards two real problems, not just the lint rule:
  // writing state after unmount, and a slow response for an old window
  // landing after a fast one for the new window and overwriting it.
  useEffect(() => {
    let cancelled = false;

    (async () => {
      try {
        const data = await api.observability(windowHours);
        if (cancelled) return;
        setSummary(data);
        setError(null);
      } catch (err) {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : 'Failed to load metrics');
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [windowHours, reloadToken]);

  const refresh = useCallback(() => {
    setLoading(true);
    setReloadToken((n) => n + 1);
  }, []);

  const costData = (summary?.pods ?? []).map((pod) => ({
    pod: pod.pod.replace(/_/g, ' '),
    key: pod.pod,
    cost: Number(pod.total_cost_usd.toFixed(5)),
    saved: Number(pod.estimated_cost_saved_usd.toFixed(5)),
  }));

  const latencyData = (summary?.pods ?? []).map((pod) => ({
    pod: pod.pod.replace(/_/g, ' '),
    key: pod.pod,
    avg: Number(pod.avg_latency_ms.toFixed(1)),
    p95: Number(pod.p95_latency_ms.toFixed(1)),
  }));

  return (
    <div className="page">
      <div className="page-header">
        <div>
          <h1>Observability</h1>
          <p className="muted">
            Cost, latency, cache efficiency and measured answer quality, aggregated from every agent
            call.
          </p>
        </div>
        <div className="row">
          <select
            aria-label="Time window"
            value={windowHours}
            onChange={(e) => setWindowHours(Number(e.target.value))}
          >
            <option value={1}>Last hour</option>
            <option value={24}>Last 24 hours</option>
            <option value={168}>Last 7 days</option>
          </select>
          <button type="button" className="button" onClick={refresh}>
            Refresh
          </button>
        </div>
      </div>

      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {loading && !summary && <p className="muted">Loading metrics…</p>}

      {summary && (
        <>
          <section className="stat-grid" data-testid="stat-grid">
            <Stat label="Requests" value={String(summary.total_requests)} />
            <Stat label="LLM calls" value={String(summary.total_llm_calls)} />
            <Stat label="Tokens" value={summary.total_tokens.toLocaleString()} />
            <Stat label="Spend" value={`$${summary.total_cost_usd.toFixed(4)}`} />
            <Stat
              label="Saved by cache"
              value={`$${summary.estimated_cost_saved_usd.toFixed(4)}`}
              hint={`${(summary.overall_cache_hit_rate * 100).toFixed(1)}% hit rate`}
            />
            <Stat label="Avg latency" value={`${summary.avg_latency_ms.toFixed(0)}ms`} />
            <Stat label="p95 latency" value={`${summary.p95_latency_ms.toFixed(0)}ms`} />
            <Stat label="Error rate" value={`${(summary.error_rate * 100).toFixed(2)}%`} />
            <Stat
              label="Faithfulness"
              value={summary.avg_faithfulness === null ? '—' : summary.avg_faithfulness.toFixed(3)}
              hint="answer supported by context"
            />
            <Stat
              label="Answer relevance"
              value={
                summary.avg_answer_relevance === null
                  ? '—'
                  : summary.avg_answer_relevance.toFixed(3)
              }
              hint="addresses the question"
            />
          </section>

          {summary.pods.length > 0 && (
            <div className="grid grid-2">
              <section className="card">
                <h2>Spend by pod</h2>
                <ResponsiveContainer width="100%" height={260}>
                  <BarChart data={costData}>
                    <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
                    <XAxis dataKey="pod" tick={{ fontSize: 12 }} />
                    <YAxis tick={{ fontSize: 12 }} />
                    <Tooltip formatter={(value) => `$${Number(value ?? 0)}`} />
                    <Legend />
                    <Bar dataKey="cost" name="Spent" radius={[4, 4, 0, 0]}>
                      {costData.map((entry) => (
                        <Cell key={entry.key} fill={POD_COLORS[entry.key] ?? '#8892a6'} />
                      ))}
                    </Bar>
                    <Bar dataKey="saved" name="Saved by cache" fill="#57b894" radius={[4, 4, 0, 0]} />
                  </BarChart>
                </ResponsiveContainer>
              </section>

              <section className="card">
                <h2>Latency by pod</h2>
                <ResponsiveContainer width="100%" height={260}>
                  <BarChart data={latencyData}>
                    <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
                    <XAxis dataKey="pod" tick={{ fontSize: 12 }} />
                    <YAxis tick={{ fontSize: 12 }} unit="ms" />
                    <Tooltip formatter={(value) => `${Number(value ?? 0)}ms`} />
                    <Legend />
                    <Bar dataKey="avg" name="Average" fill="#5b8def" radius={[4, 4, 0, 0]} />
                    <Bar dataKey="p95" name="p95" fill="#e0a05b" radius={[4, 4, 0, 0]} />
                  </BarChart>
                </ResponsiveContainer>
              </section>
            </div>
          )}

          <section className="card">
            <h2>Per-pod detail</h2>
            <div className="table-scroll">
              <table className="table">
                <thead>
                  <tr>
                    <th>Pod</th>
                    <th>Requests</th>
                    <th>LLM calls</th>
                    <th>Cost</th>
                    <th>Avg latency</th>
                    <th>p95</th>
                    <th>Cache hits</th>
                    <th>Faithfulness</th>
                    <th>Retrieval loops</th>
                    <th>No-LLM share</th>
                  </tr>
                </thead>
                <tbody>
                  {summary.pods.map((pod) => (
                    <tr key={pod.pod}>
                      <td>{pod.pod.replace(/_/g, ' ')}</td>
                      <td>{pod.requests}</td>
                      <td>{pod.llm_calls}</td>
                      <td>${pod.total_cost_usd.toFixed(5)}</td>
                      <td>{pod.avg_latency_ms.toFixed(0)}ms</td>
                      <td>{pod.p95_latency_ms.toFixed(0)}ms</td>
                      <td>
                        {pod.cache_hits} ({(pod.cache_hit_rate * 100).toFixed(0)}%)
                      </td>
                      <td>{pod.avg_faithfulness?.toFixed(3) ?? '—'}</td>
                      <td>{pod.avg_retrieval_loops?.toFixed(2) ?? '—'}</td>
                      <td>
                        {pod.trained_model_share === null
                          ? '—'
                          : `${(pod.trained_model_share * 100).toFixed(0)}%`}
                      </td>
                    </tr>
                  ))}
                  {summary.pods.length === 0 && (
                    <tr>
                      <td colSpan={10} className="muted">
                        No activity in this window yet.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </section>

          {summary.top_operations.length > 0 && (
            <section className="card">
              <h2>Costliest operations</h2>
              <div className="table-scroll">
                <table className="table">
                  <thead>
                    <tr>
                      <th>Operation</th>
                      <th>Calls</th>
                      <th>Cost</th>
                      <th>Avg latency</th>
                    </tr>
                  </thead>
                  <tbody>
                    {summary.top_operations.map((op) => (
                      <tr key={op.operation}>
                        <td>
                          <code>{op.operation}</code>
                        </td>
                        <td>{op.calls}</td>
                        <td>${op.cost_usd.toFixed(5)}</td>
                        <td>{op.avg_latency_ms.toFixed(0)}ms</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          )}
        </>
      )}
    </div>
  );
}
