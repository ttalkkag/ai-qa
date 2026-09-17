const { test, expect, login } = require('./fixtures.cjs');

for (const width of [320, 390, 600, 768, 820, 1023, 1024, 1280, 1920]) {
  for (const username of ['browser-admin', 'browser-manager']) {
    test(`프로젝트 상세는 상태와 관리 영역을 구분한다 (${username}, ${width}px)`, async ({ page, seed }) => {
      await page.setViewportSize({ width, height: 844 });
      await login(page, username);
      await page.goto(`/projects/${seed.project_id}/`);
      await expect(page.locator('.project-status')).toBeVisible();
      await expect(page.locator('.project-facts')).toContainText('마지막 확인');
      await expect(page.getByRole('button', { name: '상태 새로고침', exact: true })).toBeVisible();
      await expect(page.getByRole('main').getByRole('heading', { level: 1 })).toHaveCount(0);
      await expect(page.locator('.project-work')).toContainText('AI 실행 준비');
      if (username === 'browser-admin') {
        await expect(page.getByRole('complementary', { name: '프로젝트 관리' })).toBeVisible();
        await expect(page.getByRole('button', { name: '상태 변경', exact: true })).not.toBeVisible();
        await page.locator('.project-state-settings summary').click();
        await expect(page.getByRole('button', { name: '상태 변경', exact: true })).toBeVisible();
      } else {
        await expect(page.locator('.project-management')).toHaveCount(0);
        await expect(page.locator('.project-work')).toContainText('관리자 확인 필요');
      }
      const layout = await page.locator('.project-columns').evaluate(node => {
        const primary = node.querySelector('.project-primary').getBoundingClientRect();
        const aside = node.querySelector('.project-management')?.getBoundingClientRect();
        return { overflow: document.documentElement.scrollWidth > window.innerWidth,
          columns: getComputedStyle(node).gridTemplateColumns.split(' ').length,
          factsColumns: getComputedStyle(document.querySelector('.project-facts')).gridTemplateColumns.split(' ').length,
          menuMinHeight: Math.min(...[...document.querySelectorAll('.site-header nav a')].map(a => a.getBoundingClientRect().height)),
          overlap: aside ? (window.innerWidth >= 1024 ? primary.right > aside.left : primary.bottom > aside.top) : false };
      });
      expect(layout.overflow).toBe(false);
      expect(layout.overlap).toBe(false);
      expect(layout.columns).toBe(username === 'browser-admin' && width >= 1024 ? 2 : 1);
      expect(layout.factsColumns).toBe(width >= 1024 ? 4 : 2);
      if (width < 1024) expect(layout.menuMinHeight).toBeGreaterThanOrEqual(44);
    });
  }
}

for (const viewport of [{ width: 1280, height: 900 }, { width: 390, height: 844 }, { width: 667, height: 260 }]) {
  test(`로그인 폼은 중앙 정렬되고 좁거나 낮은 화면에서도 사용할 수 있다 (${viewport.width}px)`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await page.goto('/accounts/login/');
    const main = page.getByRole('main');
    const form = main.getByRole('form', { name: '로그인', exact: true });
    await expect(form).toBeVisible();
    await expect(main.getByRole('heading')).toHaveCount(0);
    await expect(main.locator('p')).toHaveCount(0);
    await expect(form.getByLabel('아이디:', { exact: true })).toHaveAttribute('autocomplete', 'username');
    await expect(form.getByLabel('비밀번호:', { exact: true })).toHaveAttribute('autocomplete', 'current-password');
    const layout = await form.evaluate(node => {
      const rect = node.getBoundingClientRect();
      const main = node.closest('main').getBoundingClientRect();
      return {
        width: rect.width,
        horizontalOffset: Math.abs(rect.left + rect.width / 2 - window.innerWidth / 2),
        verticalOffset: Math.abs(rect.top + rect.height / 2 - main.top - main.height / 2),
        buttonWidth: node.querySelector('button').getBoundingClientRect().width,
        overlapsNavigation: rect.top < document.querySelector('.site-navigation').getBoundingClientRect().bottom,
        overlapsFooter: rect.bottom > document.querySelector('.site-footer').getBoundingClientRect().top,
        horizontalOverflow: document.documentElement.scrollWidth > window.innerWidth,
      };
    });
    expect(layout.width).toBeLessThanOrEqual(360);
    expect(layout.width).toBeGreaterThan(300);
    expect(layout.horizontalOffset).toBeLessThanOrEqual(1);
    expect(layout.verticalOffset).toBeLessThanOrEqual(1);
    expect(layout.buttonWidth).toBe(layout.width);
    expect(layout.overlapsNavigation).toBe(false);
    expect(layout.overlapsFooter).toBe(false);
    expect(layout.horizontalOverflow).toBe(false);
    await login(page);
  });
}

for (const viewport of [{ width: 1280, height: 900 }, { width: 390, height: 844 }]) {
  test(`헤더·네비·본문·푸터를 분리하고 경로로 이동한다 (${viewport.width}px)`, async ({ page }, testInfo) => {
    await page.setViewportSize(viewport);
    await login(page, 'browser-admin');
    const projectNavigation = page.getByRole('navigation', { name: '현재 위치', exact: true });
    const add = projectNavigation.getByRole('link', { name: 'Add', exact: true });
    await expect(add).toBeVisible();
    await expect(page.getByRole('banner').getByRole('link', { name: 'Add', exact: true })).toHaveCount(0);
    await expect(page.getByRole('main').getByRole('link', { name: '프로젝트 추가', exact: true })).toHaveCount(0);
    await expect(page.getByRole('main').locator('details')).toHaveCount(0);
    const addLayout = await add.evaluate(node => {
      const rect = node.getBoundingClientRect();
      const row = node.parentElement.getBoundingClientRect();
      const style = getComputedStyle(node.parentElement);
      return { height: rect.height, rightGap: row.right - parseFloat(style.paddingRight) - rect.right,
        overlap: node.parentElement.querySelector('.breadcrumbs').getBoundingClientRect().right > rect.left };
    });
    expect(addLayout.height).toBeLessThanOrEqual(32);
    expect(Math.abs(addLayout.rightGap)).toBeLessThanOrEqual(1);
    expect(addLayout.overlap).toBe(false);
    await add.click();
    await expect(projectNavigation.getByRole('link', { name: 'Add', exact: true })).toHaveCount(0);
    await page.getByRole('main').getByRole('link', { name: '프로젝트 등록 가이드', exact: true }).click();
    await expect(page).toHaveURL(/\/docs\/#project-setup$/);
    await expect(page.getByRole('heading', { name: '7. 관리자: 프로젝트 등록과 준비', exact: true })).toBeInViewport();
    await page.getByRole('link', { name: '프로젝트 추가', exact: true }).click();
    const navigation = page.getByRole('navigation', { name: '현재 위치', exact: true });
    await expect(navigation.getByRole('link', { name: 'Add Project', exact: true })).toHaveAttribute('aria-current', 'page');
    const main = page.getByRole('main');
    await expect(main.getByRole('textbox', { name: 'Name*:', exact: true })).toBeVisible();
    await expect(main.getByRole('heading')).toHaveCount(0);
    await expect(main.locator('section')).toHaveCount(0);
    for (const name of ['name', 'description', 'local_path', 'source_ref']) {
      await expect(main.locator(`#id_${name}_helptext`)).toBeVisible();
      await expect(main.locator(`#id_${name}`)).toHaveAttribute('aria-describedby', `id_${name}_helptext`);
    }
    await expect(main.locator('#id_local_path_helptext')).toContainText('GitHub URL이 아닌');
    await expect(main.getByLabel('Branch:', { exact: true })).not.toHaveAttribute('placeholder');
    await expect(main.getByLabel('Branch:', { exact: true })).toHaveValue('');
    await expect(main.locator('#id_source_ref_helptext')).toContainText('develop → main → master → 현재 브랜치');
    await expect(main.locator('#id_local_path')).toHaveAttribute('aria-describedby', 'id_local_path_helptext');
    await expect(main.getByRole('heading', { level: 1 })).toHaveCount(0);
    await expect(main.getByRole('link', { name: '돌아가기', exact: true })).toHaveCount(0);
    await expect(main.getByRole('checkbox')).toHaveCount(0);
    const formLayout = await main.locator('form').evaluate(form => {
      return {
        fieldCount: form.querySelectorAll('.form-field').length,
        buttonHeight: form.querySelector('button').getBoundingClientRect().height,
        inputHeight: form.querySelector('input[type=text]').getBoundingClientRect().height,
        hiddenCsrf: form.querySelector('[name=csrfmiddlewaretoken]').type,
      };
    });
    expect(formLayout.fieldCount).toBe(4);
    expect(formLayout.buttonHeight).toBeGreaterThanOrEqual(viewport.width < 650 ? 40 : 34);
    expect(formLayout.buttonHeight).toBeLessThanOrEqual(viewport.width < 650 ? 44 : 38);
    expect(formLayout.inputHeight).toBeLessThanOrEqual(viewport.width < 650 ? 44 : 40);
    expect(formLayout.hiddenCsrf).toBe('hidden');
    const actionLayout = await main.locator('.form-actions').evaluate(row => {
      const form = row.closest('form').getBoundingClientRect();
      const guide = row.querySelector('a').getBoundingClientRect();
      const button = row.querySelector('button').getBoundingClientRect();
      return { rightGap: form.right - button.right, guideGap: button.left - guide.right,
        centerGap: Math.abs(guide.top + guide.height / 2 - button.top - button.height / 2),
        isLast: row === row.parentElement.lastElementChild };
    });
    expect(Math.abs(actionLayout.rightGap)).toBeLessThanOrEqual(1);
    expect(actionLayout.guideGap).toBeGreaterThanOrEqual(8);
    expect(actionLayout.centerGap).toBeLessThanOrEqual(1);
    expect(actionLayout.isLast).toBe(true);
    const layout = await page.evaluate(() => {
      const header = document.querySelector('.site-header');
      const navigation = document.querySelector('.site-navigation');
      const main = document.querySelector('.site-main');
      const headerStyle = getComputedStyle(header);
      const navigationStyle = getComputedStyle(navigation);
      return {
        regions: [...document.body.children].filter(node => ['HEADER', 'NAV', 'MAIN', 'FOOTER'].includes(node.tagName)).map(node => node.tagName),
        headerNavigationGap: navigation.getBoundingClientRect().top - header.getBoundingClientRect().bottom,
        navigationMainGap: main.getBoundingClientRect().top - navigation.getBoundingClientRect().bottom,
        headerPadding: parseFloat(headerStyle.paddingTop),
        navigationPadding: parseFloat(navigationStyle.paddingTop),
        headerBackground: headerStyle.backgroundColor,
        navigationBackground: navigationStyle.backgroundColor,
        bodyBackground: getComputedStyle(document.body).backgroundColor,
        contentLeftEdges: ['.brand', '.breadcrumbs', '.site-main > form', '.site-footer small']
          .map(selector => document.querySelector(selector).getBoundingClientRect().left),
        horizontalOverflow: document.documentElement.scrollWidth > window.innerWidth,
      };
    });
    expect(layout.regions).toEqual(['HEADER', 'NAV', 'MAIN', 'FOOTER']);
    expect(Math.abs(layout.headerNavigationGap)).toBeLessThanOrEqual(1);
    expect(layout.navigationMainGap).toBeGreaterThanOrEqual(0);
    expect(layout.navigationMainGap).toBeLessThanOrEqual(24);
    expect(layout.headerPadding).toBeLessThanOrEqual(12);
    expect(layout.navigationPadding).toBeLessThanOrEqual(8);
    expect(layout.navigationBackground).not.toBe(layout.headerBackground);
    expect(layout.navigationBackground).not.toBe(layout.bodyBackground);
    expect(Math.max(...layout.contentLeftEdges) - Math.min(...layout.contentLeftEdges)).toBeLessThanOrEqual(1);
    expect(layout.horizontalOverflow).toBe(false);
    await testInfo.attach(`project-form-${viewport.width}`, { body: await page.screenshot({ fullPage: true }), contentType: 'image/png' });
    await navigation.getByRole('link', { name: 'Project', exact: true }).click();
    await expect(page).toHaveURL(/\/projects\/$/);
    await expect(page.getByRole('navigation', { name: '주 메뉴', exact: true }).getByRole('link', { name: 'Project', exact: true })).toHaveAttribute('aria-current', 'page');
    await expect(page.getByRole('contentinfo')).toContainText('© kimchanhyung98');
  });

  test(`관리 화면의 표와 폼은 좁은 화면에서도 정렬을 유지한다 (${viewport.width}px)`, async ({ page, seed }, testInfo) => {
    await page.setViewportSize(viewport);
    await login(page, 'browser-admin');
    await page.getByRole('navigation', { name: '주 메뉴', exact: true }).getByRole('link', { name: 'AI', exact: true }).click();
    const profiles = page.locator('.model-profiles');
    const validationForms = profiles.locator('form.profile-validation');
    await expect(validationForms).toHaveCount(3);
    for (const form of await validationForms.all()) {
      await expect(form.getByRole('checkbox')).toHaveCount(0);
      await expect(form.locator('[name=consent]')).toHaveValue('');
      const metrics = await form.evaluate(element => {
        const style = getComputedStyle(element);
        return {
          marginTop: parseFloat(style.marginTop), marginBottom: parseFloat(style.marginBottom),
          buttonHeight: element.querySelector('button').getBoundingClientRect().height,
        };
      });
      expect(metrics.marginTop).toBe(0);
      expect(metrics.marginBottom).toBe(0);
      expect(metrics.buttonHeight).toBeGreaterThanOrEqual(viewport.width < 650 ? 40 : 34);
      expect(metrics.buttonHeight).toBeLessThanOrEqual(viewport.width < 650 ? 44 : 38);
    }
    const profileForm = profiles.locator('form.form-stack');
    await profiles.locator('.profile-editor summary').click();
    await expect(profileForm.locator('.form-field')).toHaveCount(4);
    await expect(profileForm.getByRole('checkbox')).toBeChecked();
    await profileForm.locator('.checkbox-field label').click();
    await expect(profileForm.getByRole('checkbox')).not.toBeChecked();
    const tableWidth = await page.getByRole('region', { name: '최근 호출 기록표', exact: true }).evaluate(region => ({
      wrapper: region.getBoundingClientRect().width,
      table: region.querySelector('table').getBoundingClientRect().width,
    }));
    expect(tableWidth.table).toBeGreaterThanOrEqual(tableWidth.wrapper - 1);
    await testInfo.attach(`ai-settings-${viewport.width}`, { body: await page.screenshot({ fullPage: true }), contentType: 'image/png' });
    for (const url of ['/ai/', '/admin/', '/accounts/manage/', '/operations/', `/projects/${seed.project_id}/configuration/`]) {
      expect((await page.goto(url)).status()).toBe(200);
      const overflow = await page.getByRole('main').evaluate(main => ({
        page: document.documentElement.scrollWidth > window.innerWidth,
        sections: [...main.querySelectorAll('section, .grid, .admin-directory, .form-stack')]
          .filter(element => element.getBoundingClientRect().right > window.innerWidth + 1)
          .map(element => element.className || element.tagName),
      }));
      expect(overflow.page, url).toBe(false);
      expect(overflow.sections, url).toEqual([]);
    }
  });
}

test('계정별 관리자 메뉴와 매니저 접근 권한을 구분한다', async ({ page, seed }) => {
  await login(page);
  const menu = page.getByRole('navigation', { name: '주 메뉴', exact: true });
  await expect(menu.getByRole('link', { name: 'AI', exact: true })).toHaveCount(0);
  await expect(menu.getByRole('link', { name: 'Admin', exact: true })).toHaveCount(0);
  expect(await menu.getByRole('link').evaluateAll(links => links.map(link => link.getAttribute('aria-label') || link.textContent.trim()))).toEqual(['Project', 'Request', 'Docs', 'Account']);
  for (const url of ['/admin/', '/accounts/manage/', '/ai/', '/operations/', `/projects/${seed.project_id}/publication-policy/`]) {
    expect((await page.request.get(url)).status()).toBe(403);
  }
  expect((await page.request.get('/approvals/')).status()).toBe(200);
  await page.goto(`/projects/${seed.project_id}/`);
  await expect(page.locator('body')).not.toContainText(seed.source);
});

test('관리자는 계정·모델·운영·발행 정책 화면을 확인한다', async ({ page, seed }) => {
  await login(page, 'browser-admin');
  const menu = page.getByRole('navigation', { name: '주 메뉴', exact: true });
  expect(await menu.getByRole('link').evaluateAll(links => links.map(link => link.getAttribute('aria-label') || link.textContent.trim()))).toEqual(['Project', 'Request', 'AI', 'Admin', 'Docs', 'Account']);
  await menu.getByRole('link', { name: 'Admin', exact: true }).click();
  await expect(page).toHaveURL(/\/admin\/$/);
  await expect(menu.getByRole('link', { name: 'Admin', exact: true })).toHaveAttribute('aria-current', 'page');
  for (const [label, url] of [['요청 승인', '/approvals/'], ['계정 관리', '/accounts/manage/'], ['운영 상태', '/operations/']]) {
    await page.getByRole('main').getByRole('link', { name: label, exact: true }).click();
    await expect(page).toHaveURL(new RegExp(`${url}$`));
    await expect(menu.getByRole('link', { name: 'Admin', exact: true })).toHaveAttribute('aria-current', 'page');
    await page.getByRole('navigation', { name: '현재 위치', exact: true }).getByRole('link', { name: 'Admin', exact: true }).click();
  }
  for (const url of ['/accounts/manage/', '/ai/', '/approvals/', '/operations/', `/projects/${seed.project_id}/publication-policy/`]) {
    expect((await page.goto(url)).status()).toBe(200);
  }
  await page.goto('/accounts/manage/');
  const managerAccount = page.locator('section').filter({ has: page.getByRole('heading', { name: /\(browser-manager\)$/ }) });
  const accountDetails = managerAccount.locator('details').filter({ has: page.locator('summary', { hasText: '역할·활성 상태 수정' }) });
  await accountDetails.locator('summary').click();
  const activeCheckbox = accountDetails.getByRole('checkbox');
  await expect(activeCheckbox).toBeChecked();
  await accountDetails.locator('.checkbox-field label').click();
  await expect(activeCheckbox).not.toBeChecked();
  await accountDetails.locator('.checkbox-field label').click();
  await expect(activeCheckbox).toBeChecked();
  const duplicateIds = await page.getByRole('main').evaluate(main => {
    const ids = [...main.querySelectorAll('[id]')].map(element => element.id);
    return ids.filter((id, index) => ids.indexOf(id) !== index);
  });
  expect(duplicateIds).toEqual([]);
  await page.goto(`/results/${seed.bundle_id}/`);
  await expect(page.getByRole('button', { name: '관리자 발행 검토 준비' })).toHaveCount(0);
  await expect(page.locator('.result-review')).toContainText('사용자 결과 확인 대기');
});

for (const username of ['browser-manager', 'browser-admin']) {
  test(`${username}는 Docs 가이드와 Account 아이콘으로 이동한다`, async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await login(page, username);
    if (username === 'browser-manager') {
      await expect(page.getByRole('navigation', { name: '현재 위치', exact: true }).getByRole('link', { name: 'Add', exact: true })).toHaveCount(0);
      await expect(page.getByRole('main').getByRole('link', { name: '프로젝트 등록 가이드', exact: true })).toHaveCount(0);
    }
    const menu = page.getByRole('navigation', { name: '주 메뉴', exact: true });
    const docs = menu.getByRole('link', { name: 'Docs', exact: true });
    const account = menu.getByRole('link', { name: 'Account', exact: true });
    for (const [link, label] of [[docs, 'Docs'], [account, 'Account']]) {
      await expect(link).toHaveAttribute('title', label);
      await expect(link.locator('svg')).toHaveAttribute('aria-hidden', 'true');
      await expect(link).toHaveText('');
    }
    await docs.focus();
    await expect(docs).toBeFocused();
    await docs.press('Enter');
    await expect(page).toHaveURL(/\/docs\/$/);
    await expect(docs).toHaveAttribute('aria-current', 'page');
    await page.getByRole('navigation', { name: '가이드 목차', exact: true }).getByRole('link', { name: '계정과 로그아웃', exact: true }).click();
    await expect(page).toHaveURL(/\/docs\/#account$/);
    await expect(page.getByRole('heading', { name: '6. 계정과 로그아웃', exact: true })).toBeInViewport();
    await page.getByRole('main').locator('#account').getByRole('link', { name: 'Account', exact: true }).click();
    await expect(page).toHaveURL(/\/accounts\/me\/$/);
    await expect(account).toHaveAttribute('aria-current', 'page');
    await expect(page.getByRole('button', { name: '로그아웃', exact: true })).toBeVisible();
    const main = page.getByRole('main');
    const name = main.getByRole('textbox', { name: '닉네임:', exact: true });
    const accountId = main.getByRole('textbox', { name: '아이디:', exact: true });
    const originalName = await name.inputValue();
    await expect(accountId).toHaveValue(username);
    await expect(main).not.toContainText('이메일');
    await expect(main).not.toContainText('사용 가능한 프로젝트');
    await expect(main).not.toContainText('관리자에게 문의');
    await expect(main.locator('[name=role], [name=target_id], [name=username]')).toHaveCount(0);
    await expect(accountId).toHaveAttribute('readonly', '');
    await expect(accountId).not.toBeEditable();
    await expect(accountId).not.toHaveAttribute('name');
    await name.fill(`${originalName} 수정 확인`);
    await main.getByRole('button', { name: '내 정보 저장', exact: true }).click();
    await expect(page).toHaveURL(/\/accounts\/me\/$/);
    await expect(name).toHaveValue(`${originalName} 수정 확인`);
    await expect(accountId).toHaveValue(username);
    await page.reload();
    await expect(name).toHaveValue(`${originalName} 수정 확인`);
    await expect(accountId).toHaveValue(username);
    await name.fill(originalName);
    await main.getByRole('button', { name: '내 정보 저장', exact: true }).click();
    await expect(name).toHaveValue(originalName);
    await expect(accountId).toHaveValue(username);
    await main.getByRole('link', { name: '비밀번호 변경', exact: true }).click();
    await expect(page).toHaveURL(/\/accounts\/password\/$/);
    await expect(page.getByLabel('현재 비밀번호:', { exact: true })).toBeVisible();
    await account.click();
    await expect(page).toHaveURL(/\/accounts\/me\/$/);
    await docs.click();
    await page.getByRole('main').getByRole('link', { name: 'Project', exact: true }).click();
    await expect(page).toHaveURL(/\/projects\/$/);
    await account.click();
    await expect(page).toHaveURL(/\/accounts\/me\/$/);
  });
}

test('같은 프로젝트의 다른 매니저도 소유하지 않은 요청·결과·파일을 볼 수 없다', async ({ page, seed }) => {
  await login(page, 'browser-other');
  for (const url of [
    `/conversations/${seed.conversation_id}/`, `/requests/${seed.request_id}/`,
    `/results/${seed.bundle_id}/`, `/artifacts/${seed.artifact_id}/download/`,
  ]) {
    expect((await page.request.get(url)).status()).toBe(404);
  }
});

test('CSRF 없는 변경과 매니저의 발행 준비를 거절한다', async ({ page, seed, baseURL }) => {
  await login(page);
  expect((await page.request.post('/accounts/logout/')).status()).toBe(403);
  await page.getByRole('navigation', { name: '주 메뉴', exact: true }).locator('a[href="/accounts/me/"]').click();
  const csrf = await page.locator('[name=csrfmiddlewaretoken]').first().inputValue();
  const forbidden = await page.request.post(`/runs/${seed.run_id}/publication/prepare/`, {
    form: { csrfmiddlewaretoken: csrf, idempotency_key: 'forbidden-browser-publication' },
    headers: { Origin: baseURL },
  });
  expect(forbidden.status()).toBe(403);
});

test('매니저는 모바일 화면에서 합성 결과와 로컬 패치를 확인한다 (실제 AI 검증 아님)', async ({ page, seed }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await login(page);
  await page.goto(`/results/${seed.bundle_id}/`);
  await expect(page.locator('body')).toContainText('합성 결과 — 실제 AI 작업이 아닙니다.');
  await expect(page.getByRole('button', { name: '관리자 발행 검토 준비' })).toHaveCount(0);
  const response = await page.request.get(`/artifacts/${seed.artifact_id}/download/`);
  expect(response.status()).toBe(200);
  expect(response.headers()['content-disposition']).toMatch(/^attachment;/);
  expect(response.headers()['cache-control']).toMatch(/no-store/);
  expect(await response.text()).toContain('+value = 2');
  await testInfo.attach('manager-result-mobile', { body: await page.screenshot({ fullPage: true }), contentType: 'image/png' });
});

test('대화 입력은 이스케이프되고 AI 미설정 시 작업자는 안전하게 실패한다', async ({ page, seed }, testInfo) => {
  await login(page);
  await page.goto(`/projects/${seed.project_id}/conversations/`);
  await expect(page.getByRole('button', { name: '대화 시작', exact: true })).toBeVisible();
  await page.getByLabel('질문 또는 현상·목표:', { exact: true }).fill(
    `${testInfo.project.name} 안내 문구 시험 <img src=x onerror="window.__e2eXss=1">`,
  );
  await page.getByRole('button', { name: '대화 시작', exact: true }).click();
  await expect(page).toHaveURL(/\/conversations\/[^/]+\/$/);
  await expect(page.locator('#response-list')).toContainText('실패', { timeout: 20_000 });
  await expect(page.locator('#response-list')).toContainText('관리자');
  await expect(page.locator('#conversation-messages')).toContainText('<img src=x onerror="window.__e2eXss=1">');
  expect(await page.evaluate(() => window.__e2eXss)).toBeUndefined();
  await expect(page.locator('#conversation-messages img')).toHaveCount(0);
});

test('매니저는 자료를 첨부하고 업무 언어로 초안을 저장한다', async ({ page, seed }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await login(page);
  await page.goto(`/projects/${seed.project_id}/conversations/`);
  await expect(page.getByRole('button', { name: '대화 시작', exact: true })).toBeVisible();
  await page.getByLabel('질문 또는 현상·목표:', { exact: true }).fill(`${testInfo.project.name} 안내 개선 요청`);
  await page.getByRole('button', { name: '대화 시작', exact: true }).click();
  await expect(page).toHaveURL(/\/conversations\/[^/]+\/$/);

  await test.step('비민감 재현 자료 첨부', async () => {
    await page.getByText('화면 자료 또는 로그 첨부', { exact: true }).click();
    await page.locator('#attachment-file').setInputFiles({
      name: 'steps.txt', mimeType: 'text/plain', buffer: Buffer.from('Synthetic reproduction steps'),
    });
    await page.getByRole('button', { name: '첨부 준비', exact: true }).click();
    await expect(page.locator('#attachment-status')).toContainText('1개 파일');
    await page.getByLabel('추가 설명:', { exact: true }).fill('비민감 첨부 설명');
    await page.getByRole('button', { name: '설명 보내기', exact: true }).click();
    await expect(page.getByRole('link', { name: '첨부 내려받기' }).first()).toBeVisible();
  });

  await test.step('초안 저장은 수정 실행이나 발행이 아니다', async () => {
    await page.getByRole('button', { name: '요청으로 정리', exact: true }).click();
    await expect(page).toHaveURL(/\/requests\/[^/]+\/$/);
    await page.locator('summary', { hasText: /^명세 보완$/ }).click();
    const attachmentLayout = await page.locator('fieldset.choice-field').evaluate(group => ({
      marginBottom: parseFloat(getComputedStyle(group).marginBottom),
      saveButtonGap: group.closest('form').querySelector('button').getBoundingClientRect().top - group.getBoundingClientRect().bottom,
    }));
    expect(attachmentLayout.marginBottom).toBeGreaterThanOrEqual(16);
    expect(attachmentLayout.saveButtonGap).toBeGreaterThanOrEqual(15.99);
    const fields = {
      '제목:': `${testInfo.project.name} 안내 개선`, '목표:': '쉽게 이해할 안내',
      '현재 동작:': '간단한 안내만 있음', '기대 동작:': '다음 행동 안내',
      '재현 방법 또는 미확인 사유:': 'example.txt 열기',
      '완료 조건 (한 줄에 하나):': '안내 문구 확인',
      '수정 범위:': 'example.txt', '제외 범위:': '원격 발행',
    };
    for (const [label, value] of Object.entries(fields)) await page.getByLabel(label, { exact: true }).fill(value);
    await page.getByRole('button', { name: '초안 저장', exact: true }).click();
    await expect(page.locator('body')).toContainText('초안 2');
    await expect(page.getByRole('button', { name: '수정 작업 시작', exact: true })).toHaveCount(0);
    await testInfo.attach('manager-draft-mobile', { body: await page.screenshot({ fullPage: true }), contentType: 'image/png' });
  });
});

test('로그아웃 후에는 이전 세션으로 결과를 읽을 수 없다', async ({ page, seed }) => {
  await login(page);
  await page.getByRole('navigation', { name: '주 메뉴', exact: true }).locator('a[href="/accounts/me/"]').click();
  await page.getByRole('button', { name: '로그아웃', exact: true }).click();
  await expect(page).toHaveURL(/\/accounts\/login\/$/);
  const response = await page.request.get(`/results/${seed.bundle_id}/`, { maxRedirects: 0 });
  expect(response.status()).toBe(302);
  expect(response.headers().location).toMatch(/^\/accounts\/login\//);
});
