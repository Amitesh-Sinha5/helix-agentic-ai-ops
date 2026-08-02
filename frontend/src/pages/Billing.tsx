import { useCallback, useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';

import { api } from '../api';
import type { Subscription, UsageResponse } from '../types';

export function Billing() {
  const [subscription, setSubscription] = useState<Subscription | null>(null);
  const [usage, setUsage] = useState<UsageResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [searchParams, setSearchParams] = useSearchParams();

  const [reloadToken, setReloadToken] = useState(0);
  const load = useCallback(() => setReloadToken((n) => n + 1), []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [sub, use] = await Promise.all([api.subscription(), api.usage()]);
        if (cancelled) return;
        setSubscription(sub);
        setUsage(use);
        setError(null);
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : 'Failed to load billing');
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [reloadToken]);

  // Stripe redirects back here after checkout. In simulated mode (no Stripe
  // keys configured) there is no webhook to fire, so the return trip completes
  // the upgrade directly — which keeps the whole flow demonstrable locally.
  useEffect(() => {
    const sessionId = searchParams.get('session_id');
    const simulated = searchParams.get('simulated') === 'true';
    if (!sessionId) return;

    (async () => {
      try {
        if (simulated) await api.completeSimulatedCheckout(sessionId);
        load();
        setNotice('Upgrade complete — you are now on the Pro plan.');
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Could not confirm the upgrade');
      } finally {
        searchParams.delete('session_id');
        searchParams.delete('simulated');
        searchParams.delete('status');
        setSearchParams(searchParams, { replace: true });
      }
    })();
  }, [searchParams, setSearchParams, load]);

  const upgrade = async () => {
    setBusy(true);
    setError(null);
    try {
      const { checkout_url } = await api.checkout();
      window.location.href = checkout_url;
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not start checkout');
      setBusy(false);
    }
  };

  const isPro = subscription?.tier === 'pro';
  const percentUsed =
    usage && !usage.unlimited && usage.limit > 0
      ? Math.min(100, (usage.used / usage.limit) * 100)
      : 0;

  return (
    <div className="page">
      <div className="page-header">
        <div>
          <h1>Billing</h1>
          <p className="muted">Your plan, current usage, and the request limit it enforces.</p>
        </div>
      </div>

      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {notice && (
        <p className="callout callout-ok" role="status">
          {notice}
        </p>
      )}

      <div className="grid grid-2">
        <section className="card" data-testid="plan-card">
          <h2>Current plan</h2>
          {subscription ? (
            <>
              <p className="plan-name">
                <span className={`badge ${isPro ? 'badge-pro' : 'badge-muted'}`}>
                  {subscription.tier.toUpperCase()}
                </span>
                <span className="muted small">status: {subscription.status}</span>
              </p>
              {subscription.current_period_end && (
                <p className="muted small">
                  Renews {new Date(subscription.current_period_end).toLocaleDateString()}
                </p>
              )}
              {!isPro && (
                <button
                  type="button"
                  className="button button-primary"
                  onClick={upgrade}
                  disabled={busy}
                >
                  {busy ? 'Redirecting…' : 'Upgrade to Pro'}
                </button>
              )}
              {isPro && <p className="muted">You have unlimited agent requests.</p>}
            </>
          ) : (
            <p className="muted">Loading…</p>
          )}
        </section>

        <section className="card" data-testid="usage-card">
          <h2>Usage</h2>
          {usage ? (
            <>
              <p className="usage-figure">
                <strong>{usage.used}</strong>
                <span className="muted"> / {usage.unlimited ? '∞' : usage.limit} requests</span>
              </p>
              {!usage.unlimited && (
                <div
                  className="progress"
                  role="progressbar"
                  aria-valuenow={usage.used}
                  aria-valuemin={0}
                  aria-valuemax={usage.limit}
                >
                  <div
                    className={`progress-bar ${percentUsed > 85 ? 'is-danger' : ''}`}
                    style={{ width: `${percentUsed}%` }}
                  />
                </div>
              )}
              <p className="muted small">
                Window resets in {Math.ceil(usage.resets_in_seconds / 3600)}h
                {usage.unlimited ? '' : ` · ${usage.remaining} remaining`}
              </p>
            </>
          ) : (
            <p className="muted">Loading…</p>
          )}
        </section>
      </div>

      <section className="card">
        <h2>Plans</h2>
        <div className="grid grid-2">
          <div className="plan">
            <h3>Free</h3>
            <p className="plan-price">$0</p>
            <ul>
              <li>20 agent requests per day</li>
              <li>All three agent pods</li>
              <li>Semantic caching</li>
            </ul>
          </div>
          <div className="plan plan-featured">
            <h3>Pro</h3>
            <p className="plan-price">$29<span className="muted">/mo</span></p>
            <ul>
              <li>Unlimited agent requests</li>
              <li>Priority support</li>
              <li>Live agent traces and full history</li>
            </ul>
            {!isPro && (
              <button type="button" className="button button-primary" onClick={upgrade} disabled={busy}>
                Upgrade to Pro
              </button>
            )}
          </div>
        </div>
      </section>
    </div>
  );
}
