import { expect, test, type Page } from '@playwright/test';

/**
 * The full product journey against real servers:
 * signup → ingest → ask (with a live agent trace) → code review → triage →
 * billing upgrade.
 *
 * Every run uses a fresh email so repeated runs against the same SQLite file do
 * not collide on the unique-email constraint.
 */

const unique = () => `e2e-${Date.now()}-${Math.floor(Math.random() * 1e4)}@helix.example.com`;

async function signup(page: Page, email: string) {
  await page.goto('/signup');
  await page.getByLabel('Email').fill(email);
  await page.getByLabel('Password').fill('e2epassw0rd');
  await page.getByRole('button', { name: /create account/i }).click();
  await expect(page.getByRole('heading', { name: /document q&a/i })).toBeVisible();
}

test.describe('Helix end-to-end journey', () => {
  test('signup, ask a grounded question, review code, triage, and upgrade', async ({ page }) => {
    const email = unique();

    // -- 1. Signup ---------------------------------------------------------
    await signup(page, email);
    await expect(page.getByText(email)).toBeVisible();

    // -- 2. Ingest a document ---------------------------------------------
    await page.getByRole('button', { name: /load sample policy/i }).click();
    await expect(page.getByText(/chunks indexed/i)).toBeVisible();

    // -- 3. Ask a question and watch the live trace ------------------------
    await page.getByRole('textbox', { name: 'Question' }).fill('How long does the free trial last?');
    await page.getByRole('button', { name: /^ask$/i }).click();

    const trace = page.getByTestId('agent-trace');
    await expect(trace).toBeVisible();

    const answer = page.getByTestId('answer');
    await expect(answer).toBeVisible({ timeout: 30_000 });
    await expect(answer).toContainText('14 days');
    await expect(answer.getByText(/groundedness/i)).toBeVisible();

    // The trace is a real reasoning trace, not a spinner.
    // The toContainText assertions auto-retry, so they must come first: a bare
    // `expect(await steps.count())` is a one-shot read and would race the last
    // few trace events still rendering.
    const steps = page.getByTestId('trace-step');
    await expect(trace).toContainText(/hybrid retriever/i);
    await expect(trace).toContainText(/answer generation/i);
    await expect(trace).toContainText(/groundedness validator/i);
    expect(await steps.count()).toBeGreaterThan(2);

    // Steps expand to reveal structured detail. Target the retriever
    // specifically: the first step is the tool router, whose only detail is
    // `tool: null`, which the component correctly renders as nothing.
    const retrieverStep = steps.filter({ hasText: 'Hybrid retriever' }).first();
    await retrieverStep.getByRole('button').click();
    await expect(retrieverStep.locator('.trace-detail')).toBeVisible();
    await expect(retrieverStep.locator('.trace-detail')).toContainText('vector hits');

    // -- 4. The same question again is served from the semantic cache ------
    await page.getByRole('textbox', { name: 'Question' }).fill('How long does the free trial last?');
    await page.getByRole('button', { name: /^ask$/i }).click();
    await expect(page.getByTestId('answer')).toBeVisible({ timeout: 30_000 });
    await expect(page.getByTestId('answer').getByText('cache hit')).toBeVisible();

    // -- 5. Code review ----------------------------------------------------
    await page.getByRole('link', { name: /code review/i }).click();
    await page.getByRole('button', { name: /review code/i }).click();

    const review = page.getByTestId('review-result');
    await expect(review).toBeVisible({ timeout: 30_000 });
    await expect(review.getByText('Request changes')).toBeVisible();
    expect(await page.getByTestId('issue').count()).toBeGreaterThan(0);

    // -- 6. Support triage -------------------------------------------------
    await page.getByRole('link', { name: /support triage/i }).click();
    await page.getByRole('button', { name: /triage ticket/i }).click();

    const triage = page.getByTestId('triage-result');
    await expect(triage).toBeVisible({ timeout: 30_000 });
    await expect(triage).toContainText(/trained model|LLM fallback/);
    await expect(triage.getByText('billing')).toBeVisible();

    // -- 7. Billing: upgrade Free → Pro ------------------------------------
    await page.getByRole('link', { name: /billing/i }).click();
    await expect(page.getByTestId('plan-card').getByText('FREE')).toBeVisible();
    await expect(page.getByTestId('usage-card').getByRole('progressbar')).toBeVisible();

    await page.getByRole('button', { name: /upgrade to pro/i }).first().click();

    // Checkout redirects back with a session id; the app completes the upgrade.
    await expect(page.getByTestId('plan-card').getByText('PRO')).toBeVisible({ timeout: 30_000 });
    await expect(page.getByTestId('plan-card')).toContainText(/unlimited agent requests/i);
  });

  test('an out-of-scope question returns an honest "not found"', async ({ page }) => {
    await signup(page, unique());
    await page.getByRole('button', { name: /load sample policy/i }).click();
    await expect(page.getByText(/chunks indexed/i)).toBeVisible();

    await page
      .getByRole('textbox', { name: 'Question' })
      .fill('What is the recommended nitrogen mix for scuba diving below 40 metres?');
    await page.getByRole('button', { name: /^ask$/i }).click();

    const answer = page.getByTestId('answer');
    await expect(answer).toBeVisible({ timeout: 30_000 });
    await expect(answer).toContainText(/could not find/i);
    await expect(answer.getByText('Sources')).toHaveCount(0);
  });

  test('the admin-only observability page is guarded', async ({ page }) => {
    // A later signup is never the first account, so this user is not an admin.
    await signup(page, unique());

    await expect(page.getByRole('link', { name: /observability/i })).toHaveCount(0);

    await page.goto('/observability');
    await expect(page.getByRole('heading', { name: /document q&a/i })).toBeVisible();
  });

  test('logging out returns to the login page and protects the app', async ({ page }) => {
    await signup(page, unique());

    await page.getByRole('button', { name: /log out/i }).click();
    await expect(page.getByRole('heading', { name: /sign in to helix/i })).toBeVisible();

    await page.goto('/doc-qa');
    await expect(page.getByRole('heading', { name: /sign in to helix/i })).toBeVisible();
  });
});
