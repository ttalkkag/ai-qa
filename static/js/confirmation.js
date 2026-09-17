(() => {
  const dialog = document.querySelector('#confirmation-dialog');
  if (!dialog || typeof dialog.showModal !== 'function') return;
  const title = dialog.querySelector('#confirmation-title');
  const defaultTitle = title.textContent;
  const message = dialog.querySelector('#confirmation-message');
  const summary = dialog.querySelector('#confirmation-summary');
  const yes = dialog.querySelector('#confirmation-yes');
  const no = dialog.querySelector('#confirmation-no');
  let pending = null;
  let approved = null;

  function clearValues(form) {
    form.querySelectorAll('[data-confirm-value]').forEach(input => { input.value = ''; });
  }

  function cancel() {
    const target = pending?.submitter;
    if (pending) clearValues(pending.form);
    pending = null;
    dialog.close();
    target?.focus();
  }

  document.addEventListener('submit', event => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || !form.hasAttribute('data-confirm')) return;
    if (approved?.form === form && approved.submitter === event.submitter) {
      approved = null;
      return;
    }
    event.preventDefault();
    event.stopImmediatePropagation();
    if (pending || !form.reportValidity()) return;
    clearValues(form);
    pending = { form, submitter: event.submitter };
    title.textContent = form.dataset.confirmTitle || defaultTitle;
    message.textContent = event.submitter?.dataset.confirm || form.dataset.confirm;
    summary.replaceChildren();
    for (const name of (form.dataset.confirmFields || '').split(/\s+/).filter(Boolean)) {
      const field = form.elements.namedItem(name);
      if (!field || ['password', 'hidden', 'file'].includes(field.type)) continue;
      const label = document.createElement('dt');
      const value = document.createElement('dd');
      label.textContent = field.labels?.[0]?.textContent || name;
      value.textContent = field.type === 'checkbox' ? (field.checked ? '예' : '아니오')
        : field instanceof HTMLSelectElement ? [...field.selectedOptions].map(option => option.textContent).join(', ')
        : field.value || '기본값 사용';
      summary.append(label, value);
    }
    yes.disabled = false;
    dialog.showModal();
    no.focus();
  }, true);

  no.addEventListener('click', cancel);
  dialog.addEventListener('keydown', event => {
    if (event.key !== 'Tab') return;
    if ((event.shiftKey && document.activeElement === no) || (!event.shiftKey && document.activeElement === yes)) {
      event.preventDefault();
      (event.shiftKey ? yes : no).focus();
    }
  });
  dialog.addEventListener('cancel', event => { event.preventDefault(); cancel(); });
  yes.addEventListener('click', () => {
    if (!pending) return;
    const { form, submitter } = pending;
    if (!form.isConnected || (submitter && !submitter.isConnected)) { cancel(); return; }
    pending = null;
    yes.disabled = true;
    dialog.close();
    if (!form.reportValidity()) return;
    form.querySelectorAll('[data-confirm-value]').forEach(input => { input.value = input.dataset.confirmValue; });
    approved = { form, submitter };
    try {
      form.requestSubmit(submitter || undefined);
    } finally {
      approved = null;
      clearValues(form);
    }
  });
  window.addEventListener('pageshow', () => {
    pending = null;
    approved = null;
    if (dialog.open) dialog.close();
    document.querySelectorAll('form[data-confirm]').forEach(clearValues);
  });
  document.querySelectorAll('form[data-confirm]').forEach(form => { form.inert = false; });
})();
