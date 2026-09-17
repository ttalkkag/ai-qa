const path = require('node:path');
const { test, expect } = require('./fixtures.cjs');

for (const editing of [false, true]) {
  test(`AI 제안 완료 시 ${editing ? '작성 중인 입력을 보존한다' : '자동 갱신한다'} (합성 응답)`, async ({ page }) => {
    let visits = 0;
    await page.route('**/progress-fixture/', route => {
      visits += 1;
      return route.fulfill({ contentType: 'text/html; charset=utf-8', body: `
        <!doctype html><html><head><meta charset="utf-8"></head><body>
        <p id="suggestion-progress" data-url="/suggestion-fixture/">처리 중</p>
        <a id="suggestion-refresh" href="/progress-fixture/" hidden>제안 보기</a>
        <form><input aria-label="목표" value="저장된 내용"></form></body></html>` });
    });
    await page.route('**/suggestion-fixture/', route => route.fulfill({ json: { suggestion_state: 'SUCCEEDED', draft_version: 1 } }));
    await page.goto('/progress-fixture/');
    await page.addScriptTag({ path: path.resolve(__dirname, '../../static/js/request-progress.js') });
    if (editing) {
      await page.getByLabel('목표').fill('저장하지 않은 보완 내용');
      await expect(page.locator('#suggestion-refresh')).toBeVisible();
      await expect(page.getByLabel('목표')).toHaveValue('저장하지 않은 보완 내용');
      expect(visits).toBe(1);
    } else {
      await expect.poll(() => visits).toBe(2);
    }
  });
}

test('실행 실패 시 새로고침 없이 재시도 버튼을 표시한다 (합성 응답)', async ({ page }) => {
  await page.route('**/run-fixture/', route => route.fulfill({ contentType: 'text/html', body: `
    <div id="run-status" data-status-url="/run-progress-fixture/"></div><ul id="run-events"></ul>
    <span id="status-label"></span><span id="status-stage"></span><span id="elapsed"></span>
    <p id="status-message"></p><p id="status-error"></p><a id="result-link" hidden></a>
    <form id="cancel-form"></form><form id="retry-form" hidden><button>Retry</button></form>` }));
  await page.route('**/run-progress-fixture/**', route => route.fulfill({ json: {
    status: 'FAILED', label: '실패', stage: 'FAILED', elapsed_seconds: 3,
    message: '새 작업으로 재시도할 수 있습니다', error_code: 'TEST_FAILURE', events: [], terminal: true,
  } }));
  await page.goto('/run-fixture/');
  await page.addScriptTag({ path: path.resolve(__dirname, '../../static/js/run-status.js') });
  await expect(page.getByRole('button', { name: 'Retry' })).toBeVisible();
  await expect(page.locator('#cancel-form')).toHaveCount(0);
});
