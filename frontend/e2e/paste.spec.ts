import { expect, test } from '@playwright/test';

const unique = () => `paste-${Date.now()}@helix.example.com`;

test('real browser: paste into the code editor works', async ({ page, context }) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write']);

  await page.goto('/signup');
  await page.getByLabel('Email').fill(unique());
  await page.getByLabel('Password').fill('pastepassw0rd');
  await page.getByRole('button', { name: /create account/i }).click();
  await expect(page.getByRole('heading', { name: /document q&a/i })).toBeVisible();

  await page.getByRole('link', { name: /code review/i }).click();
  const editor = page.getByRole('textbox', { name: 'Code' });
  await expect(editor).toHaveValue(/API_KEY/);

  // 1. Real keyboard paste (⌘V / Ctrl+V) after selecting all.
  await page.evaluate(() => navigator.clipboard.writeText('def pasted():\n    return "ok"'));
  await editor.click();
  await page.keyboard.press(process.platform === 'darwin' ? 'Meta+A' : 'Control+A');
  await page.keyboard.press(process.platform === 'darwin' ? 'Meta+V' : 'Control+V');
  await expect(editor).toHaveValue('def pasted():\n    return "ok"');

  // 2. Clear button empties it and disables submit.
  await page.getByRole('button', { name: /^clear$/i }).click();
  await expect(editor).toHaveValue('');
  await expect(page.getByRole('button', { name: /review code/i })).toBeDisabled();

  // 3. Paste-from-clipboard button replaces contents.
  await page.evaluate(() => navigator.clipboard.writeText('x = eval(user_input)'));
  await page.getByRole('button', { name: /paste from clipboard/i }).click();
  await expect(editor).toHaveValue('x = eval(user_input)');

  // 4. The pasted code actually reviews.
  await page.getByRole('button', { name: /review code/i }).click();
  await expect(page.getByTestId('review-result')).toBeVisible({ timeout: 30_000 });
  await expect(page.getByTestId('issue').first()).toContainText(/eval/i);
});
