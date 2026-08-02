/**
 * Subscribes to the live agent-trace WebSocket for one request id.
 *
 * The socket is opened *before* the HTTP request is fired (the caller generates
 * the request id up front), so the trace starts arriving as the first node runs
 * rather than after the answer is already on screen.
 */

import { useCallback, useEffect, useRef, useState } from 'react';

import { wsUrl } from '../api';
import type { TraceEvent } from '../types';

export type TraceStatus = 'idle' | 'connecting' | 'streaming' | 'closed' | 'error';

interface TraceSocketMessage {
  type: 'connected' | 'trace' | 'replay_complete' | 'heartbeat';
  event?: TraceEvent;
  count?: number;
}

export function useAgentTrace() {
  const [events, setEvents] = useState<TraceEvent[]>([]);
  const [status, setStatus] = useState<TraceStatus>('idle');
  const socketRef = useRef<WebSocket | null>(null);

  const disconnect = useCallback(() => {
    socketRef.current?.close();
    socketRef.current = null;
    setStatus((current) => (current === 'error' ? current : 'closed'));
  }, []);

  const connect = useCallback((requestId: string) => {
    socketRef.current?.close();
    setEvents([]);
    setStatus('connecting');

    let socket: WebSocket;
    try {
      socket = new WebSocket(wsUrl(`/ws/agent-status/${requestId}`));
    } catch {
      setStatus('error');
      return;
    }
    socketRef.current = socket;

    socket.onopen = () => setStatus('streaming');
    socket.onerror = () => setStatus('error');
    socket.onclose = () => setStatus((current) => (current === 'error' ? current : 'closed'));
    socket.onmessage = (raw) => {
      let message: TraceSocketMessage;
      try {
        message = JSON.parse(raw.data as string) as TraceSocketMessage;
      } catch {
        return;
      }
      if (message.type !== 'trace' || !message.event) return;

      setEvents((current) => {
        // Replay and live delivery can overlap, so de-duplicate on sequence.
        if (current.some((e) => e.sequence === message.event!.sequence)) return current;
        return [...current, message.event!].sort((a, b) => a.sequence - b.sequence);
      });
    };
  }, []);

  /**
   * Fold the authoritative trace from the HTTP response into the live view.
   *
   * The socket gives progressive updates, but closing it the moment the
   * response arrives can drop frames still in flight — so the final few steps
   * would silently never render. Every pod response carries its complete trace,
   * so merging it guarantees the finished view is correct no matter how the
   * WebSocket timing worked out. De-duplication is by `sequence`.
   */
  const merge = useCallback((incoming: TraceEvent[]) => {
    if (!incoming?.length) return;
    setEvents((current) => {
      const seen = new Set(current.map((e) => e.sequence));
      const additions = incoming.filter((e) => !seen.has(e.sequence));
      if (!additions.length) return current;
      return [...current, ...additions].sort((a, b) => a.sequence - b.sequence);
    });
  }, []);

  const reset = useCallback(() => {
    setEvents([]);
    setStatus('idle');
  }, []);

  useEffect(() => () => socketRef.current?.close(), []);

  return { events, status, connect, disconnect, merge, reset };
}

/** Collapses start/finish pairs into one row per node for display. */
export interface TraceStep {
  node: string;
  sequence: number;
  message: string;
  detail: Record<string, unknown>;
  durationMs: number | null;
  done: boolean;
}

export function toSteps(events: TraceEvent[]): TraceStep[] {
  const steps = new Map<number, TraceStep>();
  const orderByNode = new Map<string, number[]>();

  for (const event of events) {
    if (event.phase === 'start') {
      steps.set(event.sequence, {
        node: event.node,
        sequence: event.sequence,
        message: event.message,
        detail: event.detail,
        durationMs: null,
        done: false,
      });
      orderByNode.set(event.node, [...(orderByNode.get(event.node) ?? []), event.sequence]);
    } else {
      // Match a finish to the most recent unfinished start of the same node --
      // the retrieval loop means one node legitimately runs more than once.
      const candidates = orderByNode.get(event.node) ?? [];
      const openSequence = [...candidates].reverse().find((s) => steps.get(s)?.done === false);
      if (openSequence !== undefined) {
        const step = steps.get(openSequence)!;
        steps.set(openSequence, {
          ...step,
          message: event.message || step.message,
          detail: { ...step.detail, ...event.detail },
          durationMs: event.duration_ms,
          done: true,
        });
      } else {
        steps.set(event.sequence, {
          node: event.node,
          sequence: event.sequence,
          message: event.message,
          detail: event.detail,
          durationMs: event.duration_ms,
          done: true,
        });
      }
    }
  }

  return [...steps.values()].sort((a, b) => a.sequence - b.sequence);
}

export const NODE_LABELS: Record<string, string> = {
  tool_router: 'Tool router',
  retriever: 'Hybrid retriever',
  context_check: 'Context sufficiency',
  reformulate: 'Query reformulation',
  answer: 'Answer generation',
  self_critique: 'Self-critique',
  validator: 'Groundedness validator',
  semantic_cache: 'Semantic cache',
  quality_agent: 'Quality reviewer',
  security_agent: 'Security reviewer',
  summarizer: 'Summarizer',
  classifier: 'Ticket classifier',
  kb_retriever: 'Knowledge-base retriever',
  draft_agent: 'Draft reply',
  escalation_agent: 'Escalation decision',
};

export function nodeLabel(node: string): string {
  return NODE_LABELS[node] ?? node.replace(/_/g, ' ');
}
