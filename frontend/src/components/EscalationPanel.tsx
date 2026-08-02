/**
 * Admin escalation feed.
 *
 * Subscribes to /ws/admin/escalations and surfaces a ticket the moment triage
 * decides it needs a human -- no polling, no refresh.
 */

import { useEffect, useRef, useState } from 'react';

import { wsUrl } from '../api';
import { useAuth } from '../auth/AuthContext';
import type { EscalationEvent } from '../types';

const MAX_VISIBLE = 20;
const RECONNECT_BASE_MS = 1000;
const RECONNECT_MAX_MS = 30_000;

export function EscalationPanel() {
  const { isAdmin } = useAuth();
  const [escalations, setEscalations] = useState<EscalationEvent[]>([]);
  const [connected, setConnected] = useState(false);
  const [open, setOpen] = useState(true);

  const socketRef = useRef<WebSocket | null>(null);
  const attemptsRef = useRef(0);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (!isAdmin) return;
    let disposed = false;

    const connect = () => {
      if (disposed) return;
      const socket = new WebSocket(wsUrl('/ws/admin/escalations'));
      socketRef.current = socket;

      socket.onopen = () => {
        attemptsRef.current = 0;
        setConnected(true);
      };
      socket.onmessage = (raw) => {
        try {
          const message = JSON.parse(raw.data as string) as {
            type: string;
            event?: EscalationEvent;
          };
          if (message.type === 'escalation' && message.event) {
            setEscalations((current) => [message.event!, ...current].slice(0, MAX_VISIBLE));
            setOpen(true);
          }
        } catch {
          /* ignore malformed frames */
        }
      };
      socket.onclose = () => {
        setConnected(false);
        if (disposed) return;
        // Exponential backoff: a backend restart must not turn into a
        // reconnect storm from every open admin tab.
        const delay = Math.min(
          RECONNECT_MAX_MS,
          RECONNECT_BASE_MS * 2 ** attemptsRef.current++,
        );
        timerRef.current = setTimeout(connect, delay);
      };
      socket.onerror = () => socket.close();
    };

    connect();
    return () => {
      disposed = true;
      if (timerRef.current) clearTimeout(timerRef.current);
      socketRef.current?.close();
    };
  }, [isAdmin]);

  if (!isAdmin) return null;

  return (
    <aside className={`escalations ${open ? 'is-open' : 'is-collapsed'}`} data-testid="escalation-panel">
      <button type="button" className="escalations-toggle" onClick={() => setOpen(!open)}>
        <span className={`status-dot ${connected ? 'is-live' : 'is-down'}`} aria-hidden="true" />
        Escalations
        {escalations.length > 0 && <span className="badge badge-count">{escalations.length}</span>}
      </button>

      {open && (
        <div className="escalations-body">
          {escalations.length === 0 ? (
            <p className="muted">
              {connected ? 'Listening for escalations…' : 'Reconnecting…'}
            </p>
          ) : (
            <ul className="escalation-list">
              {escalations.map((event) => (
                <li key={event.ticket_id} className="escalation-item">
                  <div className="escalation-head">
                    <span className={`badge badge-${event.priority}`}>{event.priority}</span>
                    <span className="escalation-subject">{event.subject}</span>
                  </div>
                  <p className="escalation-reason">{event.reason}</p>
                  {event.suggested_owner && (
                    <p className="muted small">→ {event.suggested_owner}</p>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </aside>
  );
}
