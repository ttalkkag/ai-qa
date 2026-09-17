const { test, expect, login, confirmAction } = require('./fixtures.cjs');

test('사용자 결과 확인 후 관리자가 원격 작업 없이 로컬 승인한다', async ({ page, browser, seed }, testInfo) => {
  const fixture = seed.review_fixtures[testInfo.project.name].complete;
  await login(page);
  await page.goto(`/results/${fixture.bundle_id}/`);
  await expect(page.locator('.local-git-result')).toHaveCount(0);
  for (const width of [390, 820, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    await expect(page.getByRole('button', { name: '작업 완료 · 관리자에게 전달', exact: true })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  }
  await page.getByRole('button', { name: '작업 완료 · 관리자에게 전달', exact: true }).click();
  await confirmAction(page);
  await expect(page.locator('.result-review')).toContainText('관리자 승인 대기');
  const context = await browser.newContext({ baseURL: process.env.AIQA_E2E_BASE_URL });
  try {
    const admin = await context.newPage();
    await login(admin, 'browser-admin');
    await admin.goto(`/results/${fixture.bundle_id}/`);
    await admin.locator('.local-git-result summary').click();
    await expect(admin.locator('.local-git-result')).toContainText('temp/aiqa-browser-fixture');
    for (const width of [390, 820, 1440]) {
      await admin.setViewportSize({ width, height: 900 });
      expect(await admin.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    }
    await expect(admin.getByRole('button', { name: '작업 완료 · 관리자에게 전달', exact: true })).toHaveCount(0);
    await admin.getByRole('button', { name: '로컬 결과 승인', exact: true }).click();
    await expect(admin.getByRole('dialog')).toContainText('push·PR 생성은 실행하지 않습니다');
    await confirmAction(admin);
    await expect(admin.locator('.result-review')).toContainText('관리자 승인 완료');
    await page.reload();
    await expect(page.locator('.result-review')).toContainText('관리자 승인 완료');
  } finally { await context.close(); }
});

test('재수정 요청은 기존 결과를 유지하고 새 초안을 만든다', async ({ page, seed }, testInfo) => {
  const fixture = seed.review_fixtures[testInfo.project.name].rework;
  await login(page);
  await page.goto(`/results/${fixture.bundle_id}/`);
  await page.locator('summary', { hasText: '재수정 요청' }).click();
  await page.getByLabel('보완할 내용', { exact: true }).fill('기존 요구사항을 유지하고 사용 예시를 추가');
  await page.getByRole('button', { name: '재수정 요청', exact: true }).click();
  await confirmAction(page);
  await expect(page).toHaveURL(new RegExp(`/requests/${fixture.request_id}/$`));
  await expect(page.getByLabel('기대 동작:', { exact: true })).toHaveValue(/값 변경[\s\S]*사용 예시를 추가/);
  await expect(page.locator('body')).toContainText('초안 2');
  await page.goto(`/results/${fixture.bundle_id}/`);
  await expect(page.locator('.result-review')).toContainText('재수정 요청됨');
  await expect(page.getByRole('button', { name: '작업 완료 · 관리자에게 전달', exact: true })).toHaveCount(0);
});
