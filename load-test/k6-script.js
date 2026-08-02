/**
 * Helix load test — POST /docs/query under increasing concurrency.
 *
 * Run:
 *   k6 run load-test/k6-script.js
 *   BASE_URL=https://api.example.com k6 run load-test/k6-script.js
 *
 * The setup stage signs up a user and ingests the corpus once, then every VU
 * shares that token. Queries are drawn from a mixed pool on purpose:
 *
 *   - repeated questions exercise the semantic cache (the fast path),
 *   - unique ones force the full agent graph (the slow path),
 *   - out-of-scope ones force the re-query loop and an abstention.
 *
 * A pool of identical questions would measure the cache and call it the API.
 */

import http from 'k6/http';
import { check, group, sleep } from 'k6';
import { Counter, Rate, Trend } from 'k6/metrics';

const BASE_URL = __ENV.BASE_URL || 'http://127.0.0.1:8000';
const CACHE_RATIO = Number(__ENV.CACHE_RATIO || 0.4); // share of repeated questions

const cacheHits = new Counter('helix_cache_hits');
const cacheMisses = new Counter('helix_cache_misses');
const agentLatency = new Trend('helix_agent_latency_ms', true);
const cachedLatency = new Trend('helix_cached_latency_ms', true);
const abstentions = new Counter('helix_abstentions');
const answeredRate = new Rate('helix_answered_rate');

export const options = {
  scenarios: {
    ramp: {
      executor: 'ramping-vus',
      startVUs: 1,
      stages: [
        { duration: '20s', target: 5 },
        { duration: '30s', target: 15 },
        { duration: '30s', target: 30 },
        { duration: '20s', target: 0 },
      ],
      gracefulRampDown: '10s',
    },
  },
  thresholds: {
    http_req_failed: ['rate<0.01'],
    http_req_duration: ['p(95)<2000'],
    helix_agent_latency_ms: ['p(95)<3000'],
    checks: ['rate>0.99'],
  },
};

const CORPUS = `Helix Subscription and Refund Policy

Trial period. Every new Helix workspace begins with a free trial that lasts 14 days from the date of signup. No credit card is required to start the trial.

Refunds. Customers may request a full refund within 30 days of their first payment. Approved refunds are issued back to the original payment method within 5 to 10 business days.

Plan limits. The Free plan allows 20 agent requests per day per user. The Pro plan removes the daily request cap entirely and adds priority support. Enterprise customers receive a dedicated support engineer.

Data retention. Documents uploaded to Helix are retained for 90 days after deletion in cold storage. Audit logs are retained for 12 months.

Security. Helix is SOC 2 Type II certified. All data is encrypted at rest with AES-256 and in transit with TLS 1.3.

Deployments. Production deploys run every weekday at 14:00 UTC and require two approving reviews. The on-call rotation is one week long and changes every Monday at 10:00 UTC.`;

// Repeated across VUs -> should hit the semantic cache after the first miss.
const HOT_QUESTIONS = [
  'How long does the free trial last?',
  'How long do I have to request a refund?',
];

const COLD_QUESTIONS = [
  'How many agent requests does the Free plan allow per day?',
  'How long are documents retained after deletion?',
  'Which compliance certification does Helix hold?',
  'How is data encrypted at rest?',
  'When do production deploys run?',
  'How long is the on-call rotation?',
  'How many approving reviews does a deploy need?',
  'How long do audit logs get kept?',
  'What does the Enterprise plan include?',
  'Is a credit card required for the trial?',
];

const OUT_OF_SCOPE = [
  'What is the airspeed velocity of an unladen swallow?',
  'How do I bleed the brakes on a 1998 hatchback?',
];

function jsonHeaders(token) {
  return { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` };
}

export function setup() {
  const email = `loadtest-${Date.now()}@helix.example.com`;

  const signup = http.post(
    `${BASE_URL}/auth/signup`,
    JSON.stringify({ email, password: 'loadtest1234' }),
    { headers: { 'Content-Type': 'application/json' } },
  );
  if (signup.status !== 201) {
    throw new Error(`setup: signup failed (${signup.status}): ${signup.body}`);
  }
  const token = signup.json('tokens.access_token');

  const ingest = http.post(
    `${BASE_URL}/docs/ingest`,
    JSON.stringify({ title: 'Load Test Corpus', text: CORPUS, source: 'loadtest.md' }),
    { headers: jsonHeaders(token) },
  );
  if (ingest.status !== 201) {
    throw new Error(`setup: ingest failed (${ingest.status}): ${ingest.body}`);
  }

  return { token, chunks: ingest.json('chunk_count') };
}

export default function (data) {
  const roll = Math.random();
  let question;
  let expectAnswer = true;

  if (roll < CACHE_RATIO) {
    question = HOT_QUESTIONS[Math.floor(Math.random() * HOT_QUESTIONS.length)];
  } else if (roll < 0.95) {
    question = COLD_QUESTIONS[Math.floor(Math.random() * COLD_QUESTIONS.length)];
  } else {
    question = OUT_OF_SCOPE[Math.floor(Math.random() * OUT_OF_SCOPE.length)];
    expectAnswer = false;
  }

  group('POST /docs/query', () => {
    const response = http.post(
      `${BASE_URL}/docs/query`,
      JSON.stringify({ question, use_cache: true }),
      { headers: jsonHeaders(data.token), tags: { name: 'docs_query' } },
    );

    const ok = check(response, {
      'status is 200': (r) => r.status === 200,
      'body has an answer': (r) => {
        try {
          return typeof r.json('answer') === 'string' && r.json('answer').length > 0;
        } catch {
          return false;
        }
      },
    });

    if (!ok || response.status !== 200) return;

    const body = response.json();
    const wasCached = response.headers['X-Cache'] === 'HIT';

    if (wasCached) {
      cacheHits.add(1);
      cachedLatency.add(response.timings.duration);
    } else {
      cacheMisses.add(1);
      agentLatency.add(response.timings.duration);
    }

    answeredRate.add(body.found === true);
    if (!body.found) abstentions.add(1);

    // An out-of-scope question must abstain, not confabulate.
    if (!expectAnswer) {
      check(body, { 'out-of-scope abstains': (b) => b.found === false });
    }
  });

  sleep(0.1 + Math.random() * 0.3);
}

export function handleSummary(data) {
  const m = data.metrics;
  const pick = (name, field, fallback = 0) => m[name]?.values?.[field] ?? fallback;

  const summary = {
    generated_at: new Date().toISOString(),
    base_url: BASE_URL,
    requests: pick('http_reqs', 'count'),
    requests_per_second: Number(pick('http_reqs', 'rate').toFixed(2)),
    failed_rate: Number(pick('http_req_failed', 'rate').toFixed(5)),
    latency_ms: {
      avg: Number(pick('http_req_duration', 'avg').toFixed(1)),
      med: Number(pick('http_req_duration', 'med').toFixed(1)),
      p90: Number(pick('http_req_duration', 'p(90)').toFixed(1)),
      p95: Number(pick('http_req_duration', 'p(95)').toFixed(1)),
      max: Number(pick('http_req_duration', 'max').toFixed(1)),
    },
    agent_path_ms: {
      avg: Number(pick('helix_agent_latency_ms', 'avg').toFixed(1)),
      p95: Number(pick('helix_agent_latency_ms', 'p(95)').toFixed(1)),
    },
    cached_path_ms: {
      avg: Number(pick('helix_cached_latency_ms', 'avg').toFixed(1)),
      p95: Number(pick('helix_cached_latency_ms', 'p(95)').toFixed(1)),
    },
    cache_hits: pick('helix_cache_hits', 'count'),
    cache_misses: pick('helix_cache_misses', 'count'),
    abstentions: pick('helix_abstentions', 'count'),
    answered_rate: Number(pick('helix_answered_rate', 'rate').toFixed(4)),
    max_vus: pick('vus_max', 'max'),
  };

  return {
    stdout: `\n${JSON.stringify(summary, null, 2)}\n`,
    'load-test/results.json': JSON.stringify(summary, null, 2),
  };
}
