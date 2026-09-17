(() => {
  const list = document.getElementById("conversation-messages");
  const status = document.getElementById("conversation-status");
  if (!list || !status) return;
  const labels = {USER: "내 설명", ASSISTANT: "AI 답변", EVENT: "진행 안내"};
  let stopped = false;
  let sequence = Math.max(0, ...Array.from(list.children, element => Number(element.dataset.sequence) || 0));
  async function poll() {
    if (stopped) return;
    try {
      const response = await fetch(`${list.dataset.updatesUrl}?after=${sequence}`, {
        credentials: "same-origin", headers: {Accept: "application/json"}, cache: "no-store"
      });
      if (!response.ok || response.redirected) {
        if ([401, 403, 404].includes(response.status) || response.redirected) {
          stopped = true;
          status.textContent = "접근 권한이나 로그인 상태가 바뀌었습니다. 다시 로그인해 주세요.";
          return;
        }
        throw new Error("updates unavailable");
      }
      const data = await response.json();
      for (const message of data.messages) {
        if (message.sequence <= sequence) continue;
        const article = document.createElement("article");
        article.dataset.sequence = message.sequence;
        const heading = document.createElement("h2");
        heading.textContent = labels[message.kind] || "안내";
        const body = document.createElement("p");
        body.className = "message";
        body.textContent = message.body;
        article.append(heading, body);
        list.append(article);
        sequence = message.sequence;
      }
      const responseList = document.getElementById("response-list");
      if (responseList) {
        const cards = new Map(Array.from(responseList.children, card => [card.dataset.responseId, card]));
        for (const call of data.responses) {
          let card = cards.get(call.id);
          if (!card) {
            card = document.createElement("div");
            card.dataset.responseId = call.id;
            const label = document.createElement("p");
            label.dataset.responseLabel = "";
            const message = document.createElement("p");
            message.dataset.responseMessage = "";
            card.append(label, message);
            responseList.append(card);
          }
          card.querySelector("[data-response-label]").textContent = `답변 상태: ${call.label}`;
          card.querySelector("[data-response-message]").textContent = call.message;
        }
      }
      const waiting = data.responses.some(call => ["QUEUED", "RUNNING"].includes(call.state));
      const runStatus = document.getElementById("conversation-run-status");
      if (runStatus && data.latest_run) {
        runStatus.replaceChildren();
        const link = document.createElement("a");
        link.href = `/runs/${encodeURIComponent(data.latest_run.id)}/`;
        link.textContent = `연결된 수정 작업: ${data.latest_run.label}`;
        runStatus.append(link);
      }
      const failed = data.responses.some(call => call.retryable);
      status.textContent = waiting ? "메시지를 저장했습니다. 답변을 기다리는 중입니다." : failed
        ? "완료하지 못한 답변이 있습니다. 화면을 새로 열어 다시 요청할 수 있습니다."
        : "저장된 답변을 모두 확인했습니다.";
      if (data.readonly) {
        status.textContent = "보관된 프로젝트입니다. 이전 대화만 확인할 수 있습니다.";
        document.querySelectorAll("form button, form textarea").forEach(element => { element.disabled = true; });
      }
    } catch (_) {
      status.textContent = "상태를 불러오지 못했습니다. 잠시 후 다시 확인합니다.";
    }
    if (!stopped) window.setTimeout(poll, document.hidden ? 10000 : 2000);
  }
  const upload = document.getElementById("attachment-upload");
  const attachmentInput = document.getElementById("id_attachment_ids");
  if (upload && attachmentInput) upload.addEventListener("submit", async event => {
    event.preventDefault();
    const uploadStatus = document.getElementById("attachment-status");
    const selected = attachmentInput.value.split(",").filter(Boolean);
    if (selected.length >= 3) {
      uploadStatus.textContent = "한 메시지에 첨부는 3개까지 선택할 수 있습니다.";
      return;
    }
    const button = upload.querySelector("button");
    button.disabled = true;
    try {
      const response = await fetch(upload.action, {method: "POST", credentials: "same-origin", headers: {Accept: "application/json"}, body: new FormData(upload)});
      const data = await response.json();
      if (!response.ok) throw new Error(data.message || "첨부를 준비하지 못했습니다.");
      if (data.visibility !== "NORMAL") throw new Error("민감한 내용이 포함된 첨부입니다. 비밀정보를 제거한 새 파일을 선택해 주세요.");
      selected.push(data.id);
      attachmentInput.value = selected.join(",");
      uploadStatus.textContent = `${selected.length}개 파일을 준비했습니다. 설명을 보내면 첨부됩니다.`;
      upload.reset();
    } catch (error) {
      uploadStatus.textContent = error.message || "첨부를 준비하지 못했습니다.";
    } finally {
      button.disabled = false;
    }
  });
  window.setTimeout(poll, 2000);
})();
