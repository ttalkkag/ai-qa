const { test, expect, login } = require('./fixtures.cjs');

for (const width of [320, 820, 1920]) {
  test(`네비 높이는 버튼과 긴 경로에 관계없이 고정된다 (${width}px)`, async ({ page, seed }) => {
    await page.setViewportSize({ width, height: 900 });
    await login(page, 'browser-admin');
    for (const url of ['/projects/', '/requests/', '/admin/', `/projects/${seed.project_id}/`]) {
      await page.goto(url);
      expect(await page.locator('.site-navigation').evaluate(node => node.getBoundingClientRect().height)).toBe(48);
    }
    // Display-only stress data, without changing the stored project name.
    await page.locator('.breadcrumbs a').last().evaluate(node => { node.textContent = '긴프로젝트경로'.repeat(60); });
    const layout = await page.locator('.site-navigation').evaluate(node => ({
      height: node.getBoundingClientRect().height,
      scrollable: node.querySelector('.breadcrumbs').scrollWidth > node.querySelector('.breadcrumbs').clientWidth,
      overflow: document.documentElement.scrollWidth > innerWidth,
    }));
    expect(layout).toEqual({ height: 48, scrollable: true, overflow: false });
    await page.locator('.breadcrumbs a').last().focus();
    await expect(page.locator('.breadcrumbs a').last()).toBeFocused();
  });
}

test.describe('터치 입력 환경', () => {
  test.use({ hasTouch: true });
  for (const viewport of [{ width: 390, height: 844 }, { width: 1024, height: 768 }, { width: 1366, height: 1024 }]) {
    test(`가로 태블릿에서도 터치 영역을 유지한다 (${viewport.width}px)`, async ({ page, seed }) => {
      await page.setViewportSize(viewport);
      await login(page, 'browser-admin');
      await page.goto(`/projects/${seed.project_id}/`);
      const sizes = await page.evaluate(() => ({
        coarse: matchMedia('(pointer: coarse)').matches,
        menu: [...document.querySelectorAll('.site-header nav a')].map(node => node.getBoundingClientRect().height),
        refresh: document.querySelector('.project-statusbar button').getBoundingClientRect().height,
        overflow: document.documentElement.scrollWidth > innerWidth,
      }));
      expect(sizes.coarse).toBe(true);
      expect(Math.min(...sizes.menu)).toBeGreaterThanOrEqual(44);
      expect(sizes.refresh).toBeGreaterThanOrEqual(44);
      expect(sizes.overflow).toBe(false);
    });
  }
});

for (const viewport of [{ width: 320, height: 740 }, { width: 820, height: 1180 }, { width: 1920, height: 1080 }]) {
  test(`공통 페이지와 긴 프로젝트 정보가 화면 폭을 유지한다 (${viewport.width}px)`, async ({ page, seed }) => {
    await page.setViewportSize(viewport);
    await login(page, 'browser-admin');
    for (const url of ['/projects/', '/projects/new/', '/requests/', '/accounts/me/', '/docs/', '/ai/', '/admin/', '/accounts/manage/', '/operations/', `/projects/${seed.project_id}/configuration/`, `/projects/${seed.project_id}/edit/`, `/projects/${seed.project_id}/dependencies/`, `/projects/${seed.project_id}/publication-policy/`, `/projects/${seed.project_id}/conversations/`, `/conversations/${seed.conversation_id}/`, `/requests/${seed.request_id}/`, `/runs/${seed.run_id}/`, `/results/${seed.bundle_id}/`, '/accounts/password/', '/approvals/']) {
      expect((await page.goto(url)).status(), url).toBe(200);
      const layout = await page.evaluate(() => {
        const rect = selector => document.querySelector(selector).getBoundingClientRect();
        const header = rect('.site-header');
        const nav = rect('.site-navigation');
        const main = rect('.site-main');
        const footer = rect('.site-footer');
        const controls = [...document.querySelectorAll('.site-header nav a')].map(node => node.getBoundingClientRect());
        const frames = [...document.querySelectorAll('.header-content, .navigation-content, .site-notifications:not([hidden]) > .container, .site-main, .footer-content')].map(node => {
          const box = node.getBoundingClientRect();
          const css = getComputedStyle(node);
          return { left: box.left + parseFloat(css.paddingLeft), right: box.right - parseFloat(css.paddingRight) };
        });
        const directory = document.querySelector('.admin-directory')?.getBoundingClientRect();
        return { overflow: document.documentElement.scrollWidth > innerWidth,
          overlaps: header.bottom > nav.top + 1 || nav.bottom > main.top + 1 || main.bottom > footer.top + 1,
          headerControlOverflow: controls.some(rect => rect.left < 0 || rect.right > innerWidth + 1),
          mainWidth: main.width, frames,
          directory: directory ? { left: directory.left, right: directory.right } : null,
          centered: Math.abs(main.left - (innerWidth - main.right)) <= 1 };
      });
      expect(layout.overflow, url).toBe(false);
      expect(layout.overlaps, url).toBe(false);
      expect(layout.headerControlOverflow, url).toBe(false);
      expect(layout.mainWidth, url).toBeLessThanOrEqual(1140);
      expect(layout.centered, url).toBe(true);
      for (const frame of [...layout.frames, ...(layout.directory ? [layout.directory] : [])]) {
        expect(Math.abs(frame.left - layout.frames[0].left), `${url} left`).toBeLessThanOrEqual(1);
        expect(Math.abs(frame.right - layout.frames[0].right), `${url} right`).toBeLessThanOrEqual(1);
      }
    }
    await page.goto(`/projects/${seed.project_id}/`);
    // Synthetic display-only stress data; the project and repository are not changed.
    await page.locator('.breadcrumbs a').last().evaluate(node => { node.textContent = '긴프로젝트이름'.repeat(18); });
    await page.locator('.project-facts dd').first().evaluate(node => { node.textContent = 'feature/' + 'long-branch-'.repeat(18); });
    await page.locator('.project-repository code').evaluate(node => { node.textContent = '/Users/example/Projects/' + 'long-path-'.repeat(25); });
    const stress = await page.locator('.project-columns').evaluate(node => {
      const main = document.querySelector('.project-primary').getBoundingClientRect();
      const management = document.querySelector('.project-management').getBoundingClientRect();
      const nav = document.querySelector('.site-navigation').getBoundingClientRect();
      const overview = document.querySelector('.project-overview').getBoundingClientRect();
      return { overflow: document.documentElement.scrollWidth > innerWidth,
        navOverlap: nav.bottom > overview.top,
        gridOverlap: innerWidth >= 1024 ? main.right > management.left : main.bottom > management.top,
        columns: getComputedStyle(node).gridTemplateColumns.split(' ').length };
    });
    expect(stress.overflow).toBe(false);
    expect(stress.navOverlap).toBe(false);
    expect(stress.gridOverlap).toBe(false);
    expect(stress.columns).toBe(viewport.width >= 1024 ? 2 : 1);
  });
}
