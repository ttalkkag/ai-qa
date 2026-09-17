(() => {
  const form = document.getElementById('connection-check');
  const status = document.getElementById('connection-status');
  if (!form || !status) return;
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const button = form.querySelector('button');
    button.disabled = true;
    status.textContent = '연결 확인 중…';
    try {
      const response = await fetch(form.action, {
        method: 'POST', body: new FormData(form), headers: { Accept: 'application/json' },
      });
      if (!response.ok) throw new Error('connection check failed');
      const result = await response.json();
      status.textContent = result.connected
        ? (result.supported_cli ? '로그인 확인 · ' + result.cli_version : '로그인 확인 · CLI 버전 점검 필요')
        : (result.error_code === 'LOGIN_REQUIRED' ? '로그인 필요 · 서버에서 codex login 실행' : '연결 확인 실패 · 서버의 Codex 설정을 확인해 주세요');
    } catch {
      status.textContent = '연결 확인 실패 · 잠시 후 다시 확인해 주세요';
    } finally {
      button.disabled = false;
    }
  });
})();
