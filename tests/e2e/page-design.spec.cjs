const { test, expect, login } = require('./fixtures.cjs');

for (const width of [320, 390, 760, 820, 1440]) {
  test(`프로젝트 목록은 구분된 행과 반응형 상태 정보를 제공한다 (${width}px)`, async ({ page, seed }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    await login(page, 'browser-admin');
    const table = page.locator('.project-table');
    await expect(table).toBeVisible();
    const row = table.locator('tbody tr').filter({ has: page.locator(`a[href="/projects/${seed.project_id}/"]`) });
    await expect(row.locator('.project-chip')).toBeVisible();
    const layout = await row.evaluate(node => {
      const name = node.querySelector('.project-identity').getBoundingClientRect();
      const state = node.querySelector('[data-label="프로젝트 상태"]').getBoundingClientRect();
      return { display: getComputedStyle(node).display, below: state.top >= name.bottom, border: getComputedStyle(node.closest('.project-directory')).borderTopWidth };
    });
    expect(layout.display).toBe(width <= 760 ? 'grid' : 'table-row');
    expect(layout.below).toBe(width <= 760);
    if (width > 760) expect(layout.border).toBe('1px');
    await expect(page.getByRole('navigation', { name: '현재 위치' }).getByRole('link', { name: 'Add', exact: true })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await testInfo.attach(`projects-${width}`, { body: await page.screenshot({ fullPage: true }), contentType: 'image/png' });
    await row.locator('.project-link').click();
    await expect(page).toHaveURL(new RegExp(`/projects/${seed.project_id}/$`));
  });
}

for (const width of [390, 820, 1440]) {
  test(`주요 화면은 역할과 작업에 맞는 배치와 시작 동선을 제공한다 (${width}px)`, async ({ page, seed }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    await login(page, 'browser-admin');
    await page.goto(`/projects/${seed.project_id}/conversations/`);
    await expect(page.getByRole('navigation', { name: '현재 위치' })).toContainText('Conversations');
    await expect(page.getByRole('form', { name: '새 대화' })).toBeVisible();
    const grid = await page.locator('.workspace-grid').evaluate(node => getComputedStyle(node).gridTemplateColumns.split(' ').length);
    expect(grid).toBe(width >= 1024 ? 2 : 1);
    await expect(page.getByRole('main')).not.toContainText('비민감 시험 자료');
    const aligned = await page.locator('.conversation-composer').evaluate(form => Math.abs(form.getBoundingClientRect().right - form.querySelector('button').getBoundingClientRect().right));
    expect(aligned).toBeLessThanOrEqual(1);
    await testInfo.attach(`conversation-${width}`, { body: await page.screenshot({ fullPage: true }), contentType: 'image/png' });

    await page.goto('/ai/');
    await expect(page.locator('#connection-status')).toHaveText('확인 전');
    if (width >= 651) {
      const positions = await page.locator('.status-grid button').evaluateAll(nodes => nodes.map(node => node.getBoundingClientRect().top));
      expect(Math.abs(positions[0] - positions[1])).toBeLessThanOrEqual(1);
    }
    await expect(page.locator('.profile-editor form')).not.toBeVisible();
    await page.locator('.profile-editor summary').click();
    await expect(page.getByLabel('모델 ID:', { exact: true })).toBeVisible();
    await page.locator('.profile-editor summary').click();
    await page.route('**/ai/connection/', route => route.fulfill({ json: { connected: true, supported_cli: true, cli_version: 'codex-cli fixture' } }));
    await page.getByRole('button', { name: 'CLI·로그인 상태 확인' }).click();
    await expect(page.locator('#connection-status')).toContainText('로그인 확인');
    await expect(page).toHaveURL(/\/ai\/$/);
    await expect(page.getByRole('button', { name: 'CLI·로그인 상태 확인' })).toBeEnabled();
    await testInfo.attach(`ai-${width}`, { body: await page.screenshot({ fullPage: true }), contentType: 'image/png' });

    await page.goto('/admin/');
    await expect(page.locator('.directory-row')).toHaveCount(3);
    await expect(page.locator('.admin-directory p')).toHaveCount(3);
    await page.getByRole('main').getByRole('link', { name: '계정 관리', exact: true }).click();
    await expect(page.locator('.account-create form')).not.toBeVisible();
    await page.locator('.account-create summary').click();
    await expect(page.getByRole('button', { name: '계정 생성', exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: '로그아웃', exact: true })).toHaveCount(0);

    await page.goto('/accounts/me/');
    const account = await page.locator('.settings-page').evaluate(node => {
      const form = node.querySelector('.form-stack').getBoundingClientRect();
      const logout = node.querySelector('.account-logout').getBoundingClientRect();
      const button = node.querySelector('.form-stack button').getBoundingClientRect();
      return { width: node.getBoundingClientRect().width, formWidth: form.width, logoutWidth: logout.width, rightGap: form.right - button.right, logoutBelow: logout.top >= form.bottom };
    });
    expect(account.width).toBeLessThanOrEqual(720);
    expect(account.formWidth).toBe(account.logoutWidth);
    expect(Math.abs(account.rightGap)).toBeLessThanOrEqual(1);
    expect(account.logoutBelow).toBe(true);
    await expect(page.locator('#account-username')).toHaveAttribute('readonly', '');
    await testInfo.attach(`account-${width}`, { body: await page.screenshot({ fullPage: true }), contentType: 'image/png' });

    await page.getByRole('button', { name: '로그아웃', exact: true }).click();
    await login(page, 'browser-other');
    await page.goto('/requests/');
    await expect(page.locator('.empty-state')).toBeVisible();
    await expect(page.getByRole('link', { name: '프로젝트 보기', exact: true })).toBeVisible();
    await page.getByRole('link', { name: '프로젝트 보기', exact: true }).click();
    await expect(page).toHaveURL(/\/projects\/$/);
    await page.goto('/accounts/me/');
    await page.getByRole('button', { name: '로그아웃', exact: true }).click();
    await login(page, 'browser-manager');
    await page.goto('/requests/');
    await expect(page.locator('.request-list .status').first()).toBeVisible();
    await expect(page.locator('.empty-state')).toHaveCount(0);
  });
}

test('연결 확인 실패와 미지원 버전을 페이지 안에서 구분한다 (합성 응답)', async ({ page }) => {
  await login(page, 'browser-admin');
  await page.goto('/ai/');
  for (const [result, label] of [
    [{ connected: false, error_code: 'LOGIN_REQUIRED' }, '로그인 필요'],
    [{ connected: true, supported_cli: false }, 'CLI 버전 점검 필요'],
  ]) {
    await page.route('**/ai/connection/', route => route.fulfill({ json: result }));
    await page.getByRole('button', { name: 'CLI·로그인 상태 확인' }).click();
    await expect(page.locator('#connection-status')).toContainText(label);
    await page.unroute('**/ai/connection/');
  }
  await page.route('**/ai/connection/', route => route.fulfill({ status: 503, body: 'unavailable' }));
  await page.getByRole('button', { name: 'CLI·로그인 상태 확인' }).click();
  await expect(page.locator('#connection-status')).toContainText('잠시 후 다시 확인');
  await expect(page.getByRole('button', { name: 'CLI·로그인 상태 확인' })).toBeEnabled();
});
