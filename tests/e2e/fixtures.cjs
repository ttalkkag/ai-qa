const { test: base, expect } = require('@playwright/test');

const test = base.extend({
  baseURL: async ({}, use) => use(process.env.AIQA_E2E_BASE_URL),
  seed: async ({}, use) => use(JSON.parse(process.env.AIQA_E2E_FIXTURE)),
  launchOptions: [async ({}, use) => use({ env: {
    HOME: process.env.AIQA_E2E_BROWSER_HOME, TMPDIR: process.env.AIQA_E2E_BROWSER_HOME,
    PATH: '/usr/bin:/bin', LANG: 'C.UTF-8',
  } }), { scope: 'worker' }],
  pageErrors: [async ({ context }, use) => {
    const errors = [];
    const watch = (page) => page.on('pageerror', (error) => errors.push(String(error)));
    context.on('page', watch);
    for (const page of context.pages()) watch(page);
    await use();
    expect(errors, 'Unexpected browser JavaScript errors').toEqual([]);
  }, { auto: true }],
});

async function login(page, username = 'browser-manager') {
  await page.goto('/accounts/login/');
  await expect(page.getByRole('button', { name: '로그인', exact: true })).toBeVisible();
  await page.getByLabel('아이디:', { exact: true }).fill(username);
  await page.getByLabel('비밀번호:', { exact: true }).fill('Synthetic-browser-only-9876!');
  await page.getByRole('button', { name: '로그인', exact: true }).click();
  await expect(page).toHaveURL(/\/projects\/$/);
}

async function confirmAction(page) {
  const dialog = page.getByRole('dialog');
  await expect(dialog).toBeVisible();
  await dialog.getByRole('button', { name: '예', exact: true }).click();
}

module.exports = { test, expect, login, confirmAction };
