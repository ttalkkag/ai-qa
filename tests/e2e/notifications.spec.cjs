const { test, expect, login } = require('./fixtures.cjs');

for (const width of [390, 820, 1440]) {
  test(`알림은 새로고침에 유지되고 닫기 또는 페이지 이동으로 제거된다 (${width}px)`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await login(page, 'browser-admin');
    await page.goto('/accounts/me/');
    const notice = page.locator('.site-notifications');
    await expect(notice).toBeHidden();
    await page.getByRole('button', { name: '내 정보 저장', exact: true }).click();
    await expect(notice).toContainText('내 정보를 저장했습니다.');
    await page.reload();
    await expect(notice).toContainText('내 정보를 저장했습니다.');
    const alignment = await notice.evaluate(node => {
      const button = node.querySelector('button').getBoundingClientRect();
      const container = node.querySelector('.container');
      return { rightGap: container.getBoundingClientRect().right - parseFloat(getComputedStyle(container).paddingRight) - button.right,
        overlap: node.querySelector('.notification-messages').getBoundingClientRect().right > button.left,
        overflow: document.documentElement.scrollWidth > innerWidth };
    });
    expect(Math.abs(alignment.rightGap)).toBeLessThanOrEqual(1);
    expect(alignment.overlap).toBe(false);
    expect(alignment.overflow).toBe(false);
    await notice.getByRole('button', { name: '알림 닫기' }).click();
    await expect(notice).toBeHidden();
    await page.reload();
    await expect(notice).toBeHidden();
    await page.getByRole('button', { name: '내 정보 저장', exact: true }).click();
    await expect(notice).toBeVisible();
    await page.getByRole('link', { name: 'Request', exact: true }).click();
    await expect(notice).toBeHidden();
    await page.goBack();
    await expect(notice).toBeHidden();
    await page.reload();
    await expect(notice).toBeHidden();
    await page.getByRole('button', { name: '내 정보 저장', exact: true }).click();
    await expect(notice).toBeVisible();
    await page.getByRole('button', { name: '로그아웃', exact: true }).click();
    await expect(notice).toBeHidden();
    await login(page, 'browser-other');
    await page.goto('/accounts/me/');
    await expect(notice).toBeHidden();
  });
}

test('저장된 알림은 HTML로 실행하지 않고 잘못된 저장값은 무시한다', async ({ page }) => {
  await login(page, 'browser-admin');
  await page.goto('/accounts/me/');
  await page.getByRole('button', { name: '내 정보 저장', exact: true }).click();
  await page.evaluate(() => {
    const saved = JSON.parse(sessionStorage.getItem('aiqa.page-notification'));
    saved.messages = ['<img src=x onerror=alert(1)>', '긴 알림 '.repeat(100)];
    sessionStorage.setItem('aiqa.page-notification', JSON.stringify(saved));
  });
  await page.reload();
  await expect(page.locator('.notification-messages img')).toHaveCount(0);
  await expect(page.locator('.notification-messages')).toContainText('<img src=x onerror=alert(1)>');
  await page.evaluate(() => sessionStorage.setItem('aiqa.page-notification', '{invalid'));
  await page.reload();
  await expect(page.locator('.site-notifications')).toBeHidden();
});
