(() => {
  const status = document.querySelector('#suggestion-progress');
  if (!status) return;
  let dirty = false;
  document.querySelectorAll('form').forEach(form => form.addEventListener('input', () => { dirty = true; }));
  async function poll() {
    try {
      const response = await fetch(status.dataset.url, { credentials: 'same-origin', cache: 'no-store' });
      if (!response.ok || response.redirected) {
        status.textContent = '제안 상태를 확인할 수 없습니다 로그인과 접근 권한을 확인해 주세요';
        return;
      }
      const data = await response.json();
      if (!['QUEUED', 'RUNNING'].includes(data.suggestion_state)) {
        if (!dirty) { location.reload(); return; }
        status.textContent = '제안 처리가 끝났습니다 작성 중인 내용은 유지됩니다';
        document.querySelector('#suggestion-refresh').hidden = false;
        return;
      }
    } catch {
      status.textContent = '연결을 확인하는 중입니다 작성 중인 내용은 유지됩니다';
    }
    setTimeout(poll, 2000);
  }
  setTimeout(poll, 2000);
})();
