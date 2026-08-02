/**
 * The live reasoning trace.
 *
 * Replaces a spinner with what the agent is actually doing: which node is
 * running, what it retrieved, the rerank scores it kept, whether the
 * sufficiency check fired a re-query, and how long each step took.
 */

import { useState } from 'react';

import { nodeLabel, toSteps, type TraceStatus } from '../hooks/useAgentTrace';
import type { TraceEvent } from '../types';

interface Props {
  events: TraceEvent[];
  status: TraceStatus;
  running: boolean;
}

function formatValue(value: unknown): string {
  if (value === null || value === undefined) return '-';
  if (Array.isArray(value)) return value.length ? value.join(', ') : '-';
  if (typeof value === 'object') return JSON.stringify(value);
  if (typeof value === 'number') return Number.isInteger(value) ? String(value) : value.toFixed(3);
  return String(value);
}

function DetailGrid({ detail }: { detail: Record<string, unknown> }) {
  const entries = Object.entries(detail).filter(([, v]) => v !== null && v !== undefined);
  if (!entries.length) return null;
  return (
    <dl className="trace-detail">
      {entries.map(([key, value]) => (
        <div key={key} className="trace-detail-row">
          <dt>{key.replace(/_/g, ' ')}</dt>
          <dd>{formatValue(value)}</dd>
        </div>
      ))}
    </dl>
  );
}

export function AgentTrace({ events, status, running }: Props) {
  const [expanded, setExpanded] = useState<number | null>(null);
  const steps = toSteps(events);

  if (!steps.length) {
    if (!running) return null;
    return (
      <div className="trace" data-testid="agent-trace">
        <div className="trace-header">
          <span className="trace-title">Agent trace</span>
          <span className="badge badge-muted">{status}</span>
        </div>
        <p className="muted">Waiting for the first agent step…</p>
      </div>
    );
  }

  return (
    <div className="trace" data-testid="agent-trace">
      <div className="trace-header">
        <span className="trace-title">Agent trace</span>
        <span className={`badge ${running ? 'badge-live' : 'badge-muted'}`}>
          {running ? 'live' : `${steps.length} steps`}
        </span>
      </div>

      <ol className="trace-steps">
        {steps.map((step) => (
          <li
            key={step.sequence}
            className={`trace-step ${step.done ? 'is-done' : 'is-running'}`}
            data-testid="trace-step"
          >
            <button
              type="button"
              className="trace-step-header"
              onClick={() => setExpanded(expanded === step.sequence ? null : step.sequence)}
              aria-expanded={expanded === step.sequence}
            >
              <span className="trace-marker" aria-hidden="true">
                {step.done ? '✓' : '●'}
              </span>
              <span className="trace-node">{nodeLabel(step.node)}</span>
              <span className="trace-message">{step.message}</span>
              {step.durationMs !== null && (
                <span className="trace-duration">{step.durationMs.toFixed(0)}ms</span>
              )}
            </button>
            {expanded === step.sequence && <DetailGrid detail={step.detail} />}
          </li>
        ))}
      </ol>
    </div>
  );
}
