const fs = require('node:fs');
const path = require('node:path');
const { test, expect, login, confirmAction } = require('./fixtures.cjs');

test('게시판 기획서로 수정 요청을 접수하고 보호 점검 실패를 확인한다 (실제 AI 성공 아님)', async ({ page, seed, baseURL }, testInfo) => {
  const projectName = `${testInfo.project.name} 게시판 검색 개선`;
  const source = seed.board_sources[testInfo.project.name];
  const boardURL = JSON.parse(process.env.AIQA_E2E_BOARD_URLS)[testInfo.project.name];
  const requirements = fs.readFileSync(path.join(__dirname, '../fixtures/board/docs/requirements.md'));
  let projectURL;
  let requestURL;
  let runURL;

  await test.step('실제 더미 게시판에서 검색 미구현과 기존 읽기 동작을 확인한다', async () => {
    await page.goto(boardURL);
    await expect(page.getByRole('heading', { name: '동네 게시판', exact: true })).toBeVisible();
    await expect(page.locator('li a')).toHaveCount(3);
    await expect(page.getByRole('textbox', { name: '검색어', exact: true })).toHaveCount(0);
    await page.getByRole('link', { name: '주말 도서 모임', exact: true }).click();
    await expect(page.getByRole('heading', { name: '주말 도서 모임', exact: true })).toBeVisible();
    await expect(page.locator('body')).toContainText('토요일 오전에 함께 책을 읽습니다.');
    await page.getByRole('link', { name: '목록으로', exact: true }).click();
    await page.getByRole('link', { name: '안전한 HTML 표시', exact: true }).click();
    await expect(page.locator('body')).toContainText('<script>alert("게시판")</script>');
    await expect(page.locator('script, b')).toHaveCount(0);
    await page.goto(`${boardURL}/?q=도서`);
    await expect(page.locator('li a')).toHaveCount(3);
    await testInfo.attach('board-search-not-implemented', { body: await page.screenshot({ fullPage: true }), contentType: 'image/png' });
  });

  await test.step('관리자가 더미 Git 프로젝트를 웹에서 등록한다', async () => {
    await login(page, 'browser-admin');
    await page.getByRole('navigation', { name: '현재 위치', exact: true }).getByRole('link', { name: 'Add', exact: true }).click();
    await expect(page.getByRole('navigation', { name: '현재 위치', exact: true }).getByRole('link', { name: 'Add Project', exact: true })).toHaveAttribute('aria-current', 'page');
    await page.getByLabel('Name*:', { exact: true }).fill(projectName);
    await page.getByLabel('Description:', { exact: true }).fill('검색 기능 추가를 요청하는 비민감 더미 게시판입니다.');
    await page.getByLabel('Path*:', { exact: true }).fill(source.source);
    await expect(page.getByLabel('Branch:', { exact: true })).not.toHaveAttribute('placeholder');
    await expect(page.getByLabel('Branch:', { exact: true })).toHaveValue('');
    await page.getByRole('button', { name: '프로젝트 등록', exact: true }).click();
    await confirmAction(page);
    await expect(page.getByRole('navigation', { name: '현재 위치', exact: true }).getByRole('link', { name: projectName, exact: true })).toHaveAttribute('aria-current', 'page');
    await expect(page.locator('body')).toContainText('관리자가 필수 검증을 등록해야');
    projectURL = new URL(page.url()).pathname;
    const notice = page.getByRole('status', { name: '알림', exact: true });
    await expect(notice).toHaveText('프로젝트를 등록했습니다. 수정 실행 전에 필수 검증 명령을 등록하세요.');
    await expect(page.getByRole('main').getByRole('status')).toHaveCount(0);
    const originalViewport = page.viewportSize();
    for (const width of [1280, 390]) {
      await page.setViewportSize({ width, height: 844 });
      const layout = await notice.evaluate(node => {
        const rect = node.getBoundingClientRect();
        const nav = document.querySelector('.site-navigation').getBoundingClientRect();
        const main = document.querySelector('.site-main').getBoundingClientRect();
        return { bodyChild: node.parentElement === document.body, navGap: rect.top - nav.bottom,
          mainGap: main.top - rect.bottom, width: rect.width, viewport: window.innerWidth,
          textLeft: node.querySelector('.container').getBoundingClientRect().left + parseFloat(getComputedStyle(node.querySelector('.container')).paddingLeft),
          breadcrumbLeft: document.querySelector('.breadcrumbs').getBoundingClientRect().left,
          overflow: document.documentElement.scrollWidth > window.innerWidth };
      });
      expect(layout.bodyChild).toBe(true);
      expect(Math.abs(layout.navGap)).toBeLessThanOrEqual(1);
      expect(layout.mainGap).toBeGreaterThanOrEqual(0);
      expect(layout.width).toBe(layout.viewport);
      expect(Math.abs(layout.textLeft - layout.breadcrumbLeft)).toBeLessThanOrEqual(1);
      expect(layout.overflow).toBe(false);
      await testInfo.attach(`notification-${width}`, { body: await page.screenshot({ fullPage: true }), contentType: 'image/png' });
    }
    await page.setViewportSize(originalViewport);
    await page.reload();
    await expect(notice).toBeVisible();
    await notice.getByRole('button', { name: '알림 닫기' }).click();
    await expect(notice).toBeHidden();
  });

  await test.step('관리자가 수정 범위와 필수 검증을 설정한다', async () => {
    await page.getByRole('link', { name: '작업 정책·검증 명령', exact: true }).click();
    await expect(page.getByRole('navigation', { name: '현재 위치', exact: true }).getByRole('link', { name: 'Configuration', exact: true })).toHaveAttribute('aria-current', 'page');
    await expect(page.getByLabel('Branch:', { exact: true })).toHaveValue('main');
    await page.getByLabel('읽기 허용 경로:', { exact: true }).fill('.');
    await page.getByLabel('수정 허용 경로:', { exact: true }).fill('board.py');
    await page.getByLabel('검증 명령 목록(JSON):', { exact: true }).fill(JSON.stringify([seed.board_check]));
    await page.getByLabel('오프라인 준비 명령 목록(JSON):', { exact: true }).fill('[]');
    await page.getByRole('button', { name: '새 설정 버전 저장', exact: true }).click();
    await confirmAction(page);
    await expect(page).toHaveURL(new RegExp(`${projectURL}$`));
    await expect(page.locator('body')).toContainText('설정 2');
    await expect(page.locator('.project-checks li')).toContainText(seed.board_check.display_name);
    await expect(page.locator('.project-checks li')).toContainText('필수 · 테스트');
    await expect(page.locator('body')).not.toContainText('관리자가 필수 검증을 등록해야');
  });

  await test.step('관리자가 두 매니저에게 프로젝트 소속을 부여한다', async () => {
    await page.getByRole('link', { name: '계정·프로젝트 할당', exact: true }).click();
    for (const username of ['browser-manager', 'browser-other']) {
      const account = page.locator('section').filter({ has: page.getByRole('heading', { name: new RegExp(`\\(${username}\\)$`) }) });
      const assignment = account.locator('details').filter({ has: page.locator('summary', { hasText: '프로젝트 할당' }) });
      await assignment.locator('summary').click();
      await assignment.locator('[name=project_id]').selectOption({ label: projectName });
      await assignment.locator('[name=granted]').selectOption('yes');
      await assignment.locator('[name=reason]').fill('게시판 검색 요청과 같은 프로젝트 내 소유권 검증');
      await assignment.getByRole('button', { name: '할당 변경', exact: true }).click();
      await confirmAction(page);
      await expect(page.locator('body')).toContainText('계정 설정을 저장했습니다.');
    }
    await page.getByRole('navigation', { name: '주 메뉴', exact: true }).locator('a[href="/accounts/me/"]').click();
    await page.getByRole('button', { name: '로그아웃', exact: true }).click();
    await login(page);
    await expect(page.getByRole('link', { name: projectName, exact: true })).toBeVisible();
    await page.getByRole('link', { name: projectName, exact: true }).click();
    await expect(page.locator('body')).not.toContainText(source.source);
  });

  await test.step('매니저가 검색 기획서를 첨부하고 업무 명세를 저장한다', async () => {
    await page.getByRole('link', { name: '문제 상담·내 요청', exact: true }).click();
    await expect(page.getByRole('button', { name: '대화 시작', exact: true })).toBeVisible();
    await page.getByLabel('질문 또는 현상·목표:', { exact: true }).fill(
      '게시판에서 제목 또는 본문의 기억나는 단어로 글을 찾고 싶습니다. 기획서대로 검색을 추가하되 기존 상세와 안전한 HTML 표시를 유지해 주세요.',
    );
    await page.getByRole('button', { name: '대화 시작', exact: true }).click();
    await expect(page).toHaveURL(/\/conversations\/[^/]+\/$/);
    await page.getByText('화면 자료 또는 로그 첨부', { exact: true }).click();
    await page.locator('#attachment-file').setInputFiles({
      name: 'requirements.md', mimeType: 'text/markdown',
      buffer: requirements,
    });
    await page.getByRole('button', { name: '첨부 준비', exact: true }).click();
    await expect(page.locator('#attachment-status')).toContainText('1개 파일');
    await page.getByLabel('추가 설명:', { exact: true }).fill('첨부 기획서가 기준입니다. 원본 변경, 테스트 변경, 원격 발행은 제외합니다.');
    await page.getByRole('button', { name: '설명 보내기', exact: true }).click();
    await expect(page.getByRole('link', { name: '첨부 내려받기' }).first()).toBeVisible();
    const attachmentURL = await page.getByRole('link', { name: '첨부 내려받기' }).first().getAttribute('href');
    const attachment = await page.request.get(attachmentURL);
    expect(attachment.status()).toBe(200);
    expect(await attachment.body()).toEqual(requirements);
    await page.getByRole('button', { name: '요청으로 정리', exact: true }).click();
    await expect(page).toHaveURL(/\/requests\/[^/]+\/$/);
    requestURL = new URL(page.url()).pathname;
    await page.locator('summary', { hasText: /^명세 보완$/ }).click();
    const fields = {
      '제목:': projectName,
      '목표:': '매니저가 제목·본문의 단어로 원하는 게시글을 쉽게 찾는다.',
      '현재 동작:': '목록에 게시글 3개가 보이며 검색 입력이 없고 q 검색어를 무시한다.',
      '기대 동작:': '검색어 입력과 검색 버튼으로 제목·본문을 대소문자 무관 부분 일치 검색한다. 앞뒤 공백을 제거하고 빈 검색어는 전체 글을 표시한다.',
      '재현 방법 또는 미확인 사유:': '목록에서 검색 입력이 없는지 확인하고 /?q=도서에서도 3개가 모두 표시됨을 확인한다.',
      '완료 조건 (한 줄에 하나):': '검색어 레이블과 검색 버튼 표시\n도서 또는 토요일 검색 시 주말 도서 모임 1개\nhtml 검색 시 안전한 HTML 표시 1개\n공백 검색은 전체 3개\n불일치는 검색 결과가 없습니다. 표시\n검색어와 본문 HTML 이스케이프 유지\nbaseline_checks.py 및 change_checks.py의 실제 HTTP 검사 통과',
      '수정 범위:': '별도 작업 사본의 board.py만 수정한다.',
      '제외 범위:': '원본 저장소, baseline_checks.py, change_checks.py, docs, 게시글 CRUD, 계정, 외부 API, 보안 우회, push·PR·배포',
      '미확인 사실과 영향:': '실제 AI 수정은 실행 환경 보호 점검이 충족되어야 시작할 수 있다.',
      '먼저 해결할 필수 질문 (해결한 질문은 제거):': '',
    };
    for (const [label, value] of Object.entries(fields)) await page.getByLabel(label, { exact: true }).fill(value);
    await page.getByLabel('requirements.md', { exact: true }).check();
    await page.getByRole('button', { name: '초안 저장', exact: true }).click();
    await expect(page.locator('body')).toContainText('초안 2');
    await expect(page.getByLabel('requirements.md', { exact: true })).toBeChecked();
    await expect(page.getByRole('button', { name: '수정 작업 시작', exact: true })).toHaveCount(0);
    await testInfo.attach('board-request-draft', { body: await page.screenshot({ fullPage: true }), contentType: 'image/png' });
  });

  await test.step('명세 확인과 수정 시작을 구분하고 실제 작업자의 보호 실패를 확인한다', async () => {
    await page.getByRole('button', { name: '이 명세로 준비', exact: true }).click();
    await confirmAction(page);
    await expect(page.getByRole('button', { name: '수정 작업 시작', exact: true })).toBeVisible();
    await expect(page.locator('body')).toContainText('아직 실행하지 않은 명세입니다.');
    const revision = page.locator('details').filter({ has: page.locator('summary', { hasText: /^명세 v1 ·/ }) });
    await revision.locator('summary').click();
    await expect(revision).toContainText('별도 작업 사본의 board.py만 수정한다.');
    await page.getByRole('button', { name: '수정 작업 시작', exact: true }).click();
    await confirmAction(page);
    await expect(page).toHaveURL(/\/runs\/[^/]+\/$/);
    runURL = new URL(page.url()).pathname;
    await expect(page.locator('#status-label')).toHaveText('실패', { timeout: 20_000 });
    await expect(page.locator('#status-error')).toHaveText('DOCKER_UNAVAILABLE');
    await expect(page.locator('#run-events')).toContainText('수정 요청을 접수했습니다.');
    await expect(page.locator('#result-link')).toBeHidden();
    const status = await page.request.get(`${runURL}status/`);
    expect(status.status()).toBe(200);
    expect(status.headers()['cache-control']).toMatch(/no-store/);
    const evidence = await status.json();
    expect(evidence).toMatchObject({ status: 'FAILED', error_code: 'DOCKER_UNAVAILABLE', result_bundle_id: null, terminal: true });
    expect(evidence.events.map((event) => event.to_status)).toEqual(['QUEUED', 'PREPARING', 'FAILED']);
    await testInfo.attach('board-run-protection-failure', { body: JSON.stringify(evidence, null, 2), contentType: 'application/json' });
    await testInfo.attach('board-run-protection-failure-screen', { body: await page.screenshot({ fullPage: true }), contentType: 'image/png' });
  });

  await test.step('재시도는 새 실행이며 이후 초안 수정도 확정 명세를 바꾸지 않는다', async () => {
    await page.reload();
    const previousStatus = await (await page.request.get(`${runURL}status/`)).json();
    await page.getByRole('button', { name: '새 실행으로 재시도', exact: true }).click();
    await confirmAction(page);
    await expect(page).not.toHaveURL(new RegExp(`${runURL}$`));
    await expect(page).toHaveURL(/\/runs\/[^/]+\/$/);
    await expect(page.locator('#status-label')).toHaveText('실패', { timeout: 20_000 });
    await expect(page.locator('#status-error')).toHaveText('DOCKER_UNAVAILABLE');
    await expect(page.locator('#result-link')).toBeHidden();
    const retryURL = new URL(page.url()).pathname;
    const retry = await (await page.request.get(`${retryURL}status/`)).json();
    expect(retry.id).not.toBe(previousStatus.id);
    expect(retry).toMatchObject({ status: 'FAILED', error_code: 'DOCKER_UNAVAILABLE', result_bundle_id: null });
    expect(await (await page.request.get(`${runURL}status/`)).json()).toEqual(previousStatus);
    await testInfo.attach('board-retry-protection-failure', { body: JSON.stringify(retry, null, 2), contentType: 'application/json' });

    await page.goto(requestURL);
    const revision = page.locator('details').filter({ has: page.locator('summary', { hasText: /^명세 v1 ·/ }) });
    await revision.locator('summary').click();
    const confirmedSpec = await revision.locator('dl').innerText();
    await page.locator('summary', { hasText: /^명세 보완$/ }).click();
    await page.getByLabel('목표:', { exact: true }).fill('다음 초안만 보완하며 이미 확정한 검색 명세와 두 실행은 유지한다.');
    await page.getByRole('button', { name: '초안 저장', exact: true }).click();
    await expect(page.locator('body')).toContainText('초안 3');
    await expect(page.getByRole('button', { name: '수정 작업 시작', exact: true })).toHaveCount(0);
    await revision.locator('summary').click();
    expect(await revision.locator('dl').innerText()).toBe(confirmedSpec);
    await expect(revision.getByRole('link', { name: /처음 실행/ })).toHaveAttribute('href', runURL);
  });

  await test.step('실제 접수된 요청과 실행도 계정별 접근 권한을 적용한다', async () => {
    for (const url of ['/accounts/manage/', '/ai/', `${projectURL}configuration/`]) {
      expect((await page.request.get(url)).status()).toBe(403);
    }
    await page.getByRole('navigation', { name: '주 메뉴', exact: true }).locator('a[href="/accounts/me/"]').click();
    await page.getByRole('button', { name: '로그아웃', exact: true }).click();
    await login(page, 'browser-other');
    await expect(page.getByRole('link', { name: projectName, exact: true })).toBeVisible();
    for (const url of [requestURL, runURL, `${runURL}status/`]) {
      expect((await page.request.get(url)).status()).toBe(404);
    }
    await page.getByRole('navigation', { name: '주 메뉴', exact: true }).locator('a[href="/accounts/me/"]').click();
    const csrf = await page.locator('[name=csrfmiddlewaretoken]').first().inputValue();
    const draft = await page.request.post(`${requestURL}draft/`, {
      form: { csrfmiddlewaretoken: csrf, expected_draft_version: '3', title: '다른 매니저의 변경 시도' },
      headers: { Origin: baseURL },
    });
    expect(draft.status()).toBe(404);
    const retry = await page.request.post(`${runURL}retry/`, {
      form: { csrfmiddlewaretoken: csrf, key: `${testInfo.project.name}-forbidden-board-retry` },
      headers: { Origin: baseURL },
    });
    expect(retry.status()).toBe(404);
    await page.goto(`${boardURL}/?q=도서`);
    await expect(page.locator('li a')).toHaveCount(3);
    await expect(page.getByRole('textbox', { name: '검색어', exact: true })).toHaveCount(0);
  });
});
