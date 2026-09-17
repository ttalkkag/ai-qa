(() => {
  const region = document.querySelector('.site-notifications');
  if (!region) return;
  const content = region.querySelector('.notification-messages');
  const close = region.querySelector('.notification-close');
  const key = 'aiqa.page-notification';
  const page = location.pathname + location.search;
  const account = region.dataset.account;

  function read() {
    try {
      const saved = JSON.parse(sessionStorage.getItem(key));
      if (saved?.page === page && saved.account === account &&
          Array.isArray(saved.messages) && saved.messages.every(message => typeof message === 'string')) {
        return saved.messages;
      }
    } catch (_) {}
    return [];
  }

  function show(messages) {
    content.replaceChildren(...messages.map(message => {
      const paragraph = document.createElement('p');
      paragraph.textContent = message;
      return paragraph;
    }));
    region.hidden = messages.length === 0;
    close.hidden = region.hidden;
    try {
      if (messages.length) sessionStorage.setItem(key, JSON.stringify({ page, account, messages }));
      else sessionStorage.removeItem(key);
    } catch (_) {}
  }

  const incoming = [...content.querySelectorAll('p')].map(node => node.textContent);
  show(incoming.length ? incoming : read());
  close.addEventListener('click', () => {
    show([]);
    document.querySelector('.breadcrumbs a[aria-current="page"]')?.focus();
  });
  window.addEventListener('pageshow', event => {
    if (event.persisted) show(read());
  });
})();
