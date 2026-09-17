const { test, expect, login } = require('./fixtures.cjs');

for (const width of [1280, 390]) {
  test(`등록 입력 검증과 예·아니오·Esc 확인을 거친다 (${width}px)`, async ({ page }) => {
    await page.setViewportSize({ width, height: 844 });
    await login(page, 'browser-admin');
    await page.goto('/projects/new/');
    const posts = [];
    await page.route('**/projects/new/', async route => {
      if (route.request().method() !== 'POST') return route.continue();
      posts.push(new URLSearchParams(route.request().postData()));
      await route.fulfill({ status: 200, contentType: 'text/html; charset=utf-8', body: '<p>submitted</p>' });
    });
    const submit = page.getByRole('button', { name: '프로젝트 등록', exact: true });
    const dialog = page.getByRole('dialog');
    await submit.click();
    await expect(dialog).not.toBeVisible();
    const name = page.getByLabel('Name*:', { exact: true });
    const path = page.getByLabel('Path*:', { exact: true });
    await name.fill('<img src=x onerror=alert(1)>');
    await path.fill('https://example.invalid/repo');
    await submit.click();
    await expect(dialog).not.toBeVisible();
    expect(await path.evaluate(input => input.validity.patternMismatch)).toBe(true);
    expect(posts).toHaveLength(0);
    await path.fill('/tmp/synthetic-confirmation-only');
    const branch = page.getByLabel('Branch:', { exact: true });
    for (const value of ['Feature/auth', 'my-feature', 'temp/_testing', 'temp/a..b', 'refs/heads/plain']) {
      await branch.fill(value);
      await submit.click();
      expect(await branch.evaluate(input => input.validity.patternMismatch), value).toBe(true);
      await expect(dialog).not.toBeVisible();
      expect(posts).toHaveLength(0);
    }
    for (const value of ['', 'develop', 'temp/ai-qa-testing', 'refs/heads/fix/api/login']) {
      await branch.fill(value);
      expect(await branch.evaluate(input => input.checkValidity()), value).toBe(true);
    }
    await branch.fill('temp/ai-qa-testing');
    await submit.click();
    await expect(dialog).toBeVisible();
    await expect(dialog).toHaveAccessibleName('프로젝트 생성');
    await expect(dialog.locator('#confirmation-message')).toHaveText('프로젝트를 등록할까요?');
    await expect(dialog).toContainText('<img src=x onerror=alert(1)>');
    await expect(dialog.locator('img')).toHaveCount(0);
    await expect(dialog).toContainText('/tmp/synthetic-confirmation-only');
    const no = dialog.getByRole('button', { name: '아니오', exact: true });
    await expect(no).toBeFocused();
    await no.press('Shift+Tab');
    await expect(dialog.getByRole('button', { name: '예', exact: true })).toBeFocused();
    await page.keyboard.press('Escape');
    await expect(dialog).not.toBeVisible();
    await expect(submit).toBeFocused();
    await expect(name).toHaveValue('<img src=x onerror=alert(1)>');
    expect(posts).toHaveLength(0);
    await submit.click();
    await no.click();
    await expect(dialog).not.toBeVisible();
    expect(posts).toHaveLength(0);
    await submit.click();
    const box = await dialog.boundingBox();
    expect(box.x).toBeGreaterThanOrEqual(0);
    expect(box.x + box.width).toBeLessThanOrEqual(width);
    await dialog.getByRole('button', { name: '예', exact: true }).evaluate(button => { button.click(); button.click(); });
    await expect(page.getByText('submitted', { exact: true })).toBeVisible();
    expect(posts).toHaveLength(1);
    expect(posts[0].get('local_path')).toBe('/tmp/synthetic-confirmation-only');
    expect(posts[0].get('source_ref')).toBe('temp/ai-qa-testing');
    expect(posts[0].has('confirm_scope')).toBe(false);
    expect(posts[0].get('csrfmiddlewaretoken')).toBeTruthy();
  });
}

test('프로젝트 설정도 잘못된 브랜치명이면 확인 창을 열지 않는다', async ({ page, seed }) => {
  await login(page, 'browser-admin');
  await page.goto(`/projects/${seed.project_id}/`);
  await page.getByRole('link', { name: '작업 정책·검증 명령', exact: true }).click();
  const branch = page.getByLabel('Branch:', { exact: true });
  await branch.fill('Feature/auth');
  await page.getByRole('button', { name: '새 설정 버전 저장', exact: true }).click();
  expect(await branch.evaluate(input => input.validity.patternMismatch)).toBe(true);
  await expect(page.getByRole('dialog')).not.toBeVisible();
  await branch.fill('temp/ai-qa-testing');
  await page.getByRole('button', { name: '새 설정 버전 저장', exact: true }).click();
  await expect(page.getByRole('dialog')).toBeVisible();
  await page.getByRole('dialog').getByRole('button', { name: '아니오', exact: true }).click();
});

test('모델 검증은 예를 선택할 때만 동의 값을 전송한다 (실제 AI 호출 없음)', async ({ page }) => {
  await login(page, 'browser-admin');
  await page.goto('/ai/');
  const form = page.locator('.profile-validation').first();
  const action = await form.getAttribute('action');
  const posts = [];
  await page.route(`**${action}`, async route => {
    posts.push(new URLSearchParams(route.request().postData()));
    await route.fulfill({ status: 200, contentType: 'text/html; charset=utf-8', body: '<p>model-validation-submitted</p>' });
  });
  await expect(form.getByRole('checkbox')).toHaveCount(0);
  await form.getByRole('button', { name: '검증 등록', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog).toContainText('실제 모델 이용량');
  await dialog.getByRole('button', { name: '아니오', exact: true }).click();
  expect(posts).toHaveLength(0);
  await expect(form.locator('[name=consent]')).toHaveValue('');
  await form.getByRole('button', { name: '검증 등록', exact: true }).click();
  await dialog.getByRole('button', { name: '예', exact: true }).click();
  await expect(page.getByText('model-validation-submitted', { exact: true })).toBeVisible();
  expect(posts).toHaveLength(1);
  expect(posts[0].get('consent')).toBe('yes');
  expect(posts[0].get('key')).toBeTruthy();
});

test('확인 스크립트가 준비되지 않으면 중요한 폼은 비활성 상태다', async ({ page }) => {
  await login(page, 'browser-admin');
  await page.route('**/confirmation*.js', route => route.abort());
  await page.goto('/projects/new/');
  await expect(page.locator('form[data-confirm]')).toHaveAttribute('inert', '');
});

test('확인 후에도 선택한 버튼의 formaction과 값이 유지된다 (합성 폼)', async ({ page }) => {
  await login(page, 'browser-admin');
  await page.goto('/projects/new/');
  const scriptURL = await page.locator('script[src*="confirmation"]').getAttribute('src');
  const modalHTML = await page.locator('#confirmation-dialog').evaluate(node => node.outerHTML);
  const posts = [];
  await page.route('**/confirmation-*/', async route => {
    if (route.request().method() === 'POST') {
      posts.push({ url: route.request().url(), values: new URLSearchParams(route.request().postData()) });
      return route.fulfill({ contentType: 'text/html; charset=utf-8', body: '<p>action-submitted</p>' });
    }
    await route.fulfill({ contentType: 'text/html; charset=utf-8', body: `<!doctype html><html><head><meta charset="utf-8"></head><body>
      <form method="post" action="/confirmation-wrong/" inert data-confirm="기본 동작" data-confirm-fields="secret">
        <input name="secret" type="password" value="synthetic-password">
        <button name="decision" value="approve" formaction="/confirmation-approve/" data-confirm="승인을 진행할까요?">승인</button>
        <button name="decision" value="reject" formaction="/confirmation-reject/" data-confirm="거절을 진행할까요?">거절</button>
      </form>${modalHTML}<script src="${scriptURL}" defer></script></body></html>` });
  });
  await page.goto('/confirmation-fixture/');
  await page.getByRole('button', { name: '거절', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog).toContainText('거절을 진행할까요?');
  await expect(dialog).not.toContainText('synthetic-password');
  await dialog.getByRole('button', { name: '예', exact: true }).click();
  await expect(page.getByText('action-submitted', { exact: true })).toBeVisible();
  expect(posts).toHaveLength(1);
  expect(posts[0].url).toMatch(/\/confirmation-reject\/$/);
  expect(posts[0].values.get('decision')).toBe('reject');
});
