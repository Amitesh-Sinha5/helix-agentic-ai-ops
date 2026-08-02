import { defineConfig, devices } from '@playwright/test';

// Dedicated ports, deliberately NOT the dev (8000/5173) or docker-compose ones.
// Sharing them means `reuseExistingServer` silently reuses whatever is already
// listening -- a dev server, or a compose container with different CORS
// settings -- and the whole suite fails with confusing auth errors.
const BACKEND_URL = process.env.E2E_API_URL ?? 'http://127.0.0.1:8100';
const FRONTEND_URL = process.env.E2E_BASE_URL ?? 'http://127.0.0.1:4174';

/**
 * End-to-end config.
 *
 * Both servers are started by Playwright itself so `npx playwright test` works
 * from a clean checkout. The backend runs against SQLite with the mock LLM
 * provider, so the suite needs no database, no Redis and no API keys.
 */
export default defineConfig({
  testDir: './e2e',
  timeout: 60_000,
  expect: { timeout: 10_000 },
  fullyParallel: false, // the backend's "first user becomes admin" rule is global state
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [['github'], ['list']] : 'list',

  use: {
    baseURL: FRONTEND_URL,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },

  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],

  webServer: [
    {
      // Migrations run first: the E2E database is created from scratch, and
      // `alembic upgrade head` is the same path production uses.
      command:
        'cd ../backend && .venv/bin/alembic upgrade head && ' +
        '.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8100',
      url: `${BACKEND_URL}/health`,
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
      env: {
        LLM_PROVIDER: 'mock',
        EMBEDDING_PROVIDER: 'mock',
        DATABASE_URL: 'sqlite+aiosqlite:///./e2e.db',
        CHROMA_PERSIST_DIR: './.chroma-e2e',
        REDIS_URL: 'redis://127.0.0.1:6399/15', // unreachable -> in-process fallback
        JWT_SECRET_KEY: 'e2e-secret',
        CORS_ORIGINS: FRONTEND_URL,
        // Checkout redirects the browser to these, so they must point at the
        // frontend actually under test -- not the localhost:5173 dev default.
        BILLING_SUCCESS_URL: `${FRONTEND_URL}/billing?status=success`,
        BILLING_CANCEL_URL: `${FRONTEND_URL}/billing?status=cancelled`,
        RATE_LIMIT_ENABLED: 'false', // the E2E flow is not a rate-limit test
      },
    },
    {
      command: `npm run build && npm run preview -- --port 4174 --strictPort`,
      url: FRONTEND_URL,
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
      env: { VITE_API_URL: BACKEND_URL },
    },
  ],
});
