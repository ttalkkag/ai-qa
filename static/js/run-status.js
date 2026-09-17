(() => {
  const root = document.querySelector("#run-status");
  if (!root || root.dataset.terminal === "true") return;
  const list = document.querySelector("#run-events");
  let sequence = Math.max(0, ...Array.from(list.children, (row) => Number(row.dataset.sequence)));
  let stopped = false;
  async function poll() {
    try {
      const response = await fetch(`${root.dataset.statusUrl}?after_sequence=${sequence}`, {
        headers: { Accept: "application/json" }, credentials: "same-origin", cache: "no-store",
      });
      if (!response.ok || response.redirected) {
        stopped = [401, 403, 404].includes(response.status) || response.redirected;
        document.querySelector("#status-message").textContent = "상태를 확인하지 못했습니다. 로그인과 접근 권한을 확인해 주세요.";
        return;
      }
      const value = await response.json();
      document.querySelector("#status-label").textContent = value.label;
      document.querySelector("#status-stage").textContent = value.stage;
      document.querySelector("#elapsed").textContent = value.elapsed_seconds;
      document.querySelector("#status-message").textContent = value.message;
      document.querySelector("#status-error").textContent = value.error_code;
      for (const event of value.events) {
        if (event.sequence <= sequence) continue;
        const row = document.createElement("li");
        row.dataset.sequence = event.sequence;
        row.textContent = event.message;
        list.append(row);
        sequence = event.sequence;
      }
      if (value.result_bundle_id && /^[0-9a-f-]{36}$/.test(value.result_bundle_id)) {
        const link = document.querySelector("#result-link");
        link.href = `/results/${value.result_bundle_id}/`;
        link.hidden = false;
      }
      if (value.terminal) {
        stopped = true;
        document.querySelector("#cancel-form")?.remove();
        const retry = document.querySelector("#retry-form");
        if (retry) retry.hidden = value.status === "SUCCEEDED";
      }
    } catch {
      document.querySelector("#status-message").textContent = "연결을 확인하는 중입니다. 실행 상태는 서버에 보관됩니다.";
    } finally {
      if (!stopped) window.setTimeout(poll, document.hidden ? 10000 : 2000);
    }
  }
  window.setTimeout(poll, 2000);
})();
