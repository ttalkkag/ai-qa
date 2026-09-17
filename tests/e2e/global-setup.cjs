const assert = require('node:assert/strict');
const fs = require('node:fs');
const net = require('node:net');
const os = require('node:os');
const path = require('node:path');
const { spawn, execFileSync } = require('node:child_process');

const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function availablePort() {
  const socket = net.createServer();
  await new Promise((resolve, reject) => {
    socket.once('error', reject);
    socket.listen(0, '127.0.0.1', resolve);
  });
  const port = socket.address().port;
  await new Promise((resolve, reject) => socket.close((error) => error ? reject(error) : resolve()));
  return port;
}

async function stop(child) {
  if (!child?.pid) return;
  const signalGroup = (signal) => {
    try { process.kill(-child.pid, signal); }
    catch (error) { if (error.code !== 'ESRCH') throw error; }
  };
  signalGroup('SIGTERM');
  for (let i = 0; i < 50 && child.exitCode === null && child.signalCode === null; i++) await pause(100);
  signalGroup('SIGKILL');
  for (let i = 0; i < 10 && child.exitCode === null && child.signalCode === null; i++) await pause(100);
  assert.ok(child.exitCode !== null || child.signalCode !== null, 'E2E child did not stop');
}

module.exports = async function globalSetup() {
  const source = path.resolve(__dirname, '../..');
  const output = path.join(source, 'test-results/e2e');
  const work = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'aiqa-e2e-')));
  const isolatedHome = path.join(work, 'home');
  fs.mkdirSync(isolatedHome, { mode: 0o700 });
  const browserHome = path.join(work, 'browser-home');
  fs.mkdirSync(browserHome, { mode: 0o700 });
  fs.mkdirSync(output, { recursive: true });
  fs.writeFileSync(path.join(output, 'source-integrity.json'), JSON.stringify({ status: 'not_checked' }));
  const baseURL = `http://127.0.0.1:${await availablePort()}`;
  // Explicit allowlist: the application receives no host API keys, Codex login, or Git credentials.
  const env = {
    PATH: '/usr/bin:/bin', HOME: isolatedHome, TMPDIR: work,
    LANG: 'C.UTF-8', PYTHONUNBUFFERED: '1', PYTHONPATH: source,
    DJANGO_SETTINGS_MODULE: 'config.settings',
    AIQA_DATA_DIR: path.join(work, 'runtime'),
    AIQA_E2E_FIXTURES: path.join(work, 'fixtures'),
    AIQA_BIND: baseURL.slice('http://'.length), AIQA_PUBLIC_ORIGIN: baseURL,
    AIQA_ACCESS_MODE: 'loopback', AIQA_TRUST_PROXY: 'false', AIQA_DEBUG: 'false',
    AIQA_CODEX_BIN: path.join(work, 'codex-intentionally-unavailable'),
    AIQA_DOCKER_IMAGE: 'aiqa-intentionally-unavailable:e2e',
    GIT_CONFIG_NOSYSTEM: '1', GIT_CONFIG_GLOBAL: '/dev/null', GIT_TERMINAL_PROMPT: '0',
  };
  const python = path.join(source, '.venv/bin/python');
  const log = fs.openSync(path.join(output, 'services.log'), 'w', 0o600);
  const children = [];
  let seed;
  const command = (args) => execFileSync(python, args, {
    cwd: source, env, encoding: 'utf8', timeout: 60_000, stdio: ['ignore', 'pipe', log],
  });
  const cleanup = async () => {
    try {
      const stopped = await Promise.allSettled([...children].reverse().map(stop));
      const failures = stopped.filter((result) => result.status === 'rejected');
      if (failures.length) throw new AggregateError(failures.map((result) => result.reason), 'E2E service cleanup failed');
      if (seed) {
        for (const fixture of [seed, ...Object.values(seed.board_sources)]) {
          const git = (args) => execFileSync('/usr/bin/git', ['-C', fixture.source, ...args], {
            env, encoding: 'utf8', timeout: 10_000,
          }).trim();
          assert.equal(git(['status', '--porcelain']), '', 'Browser actions changed the source fixture');
          assert.equal(git(['rev-parse', 'HEAD']), fixture.source_commit, 'Browser actions changed the source commit');
          assert.equal(git(['remote']), '', 'The fixture must never have a remote');
        }
        fs.writeFileSync(path.join(output, 'source-integrity.json'), JSON.stringify({
          status: 'passed', clean: true, commitUnchanged: true, remoteCount: 0,
          boardRepositories: Object.keys(seed.board_sources).length,
          boundary: 'real HTTP/browser/SQLite; synthetic result; no actual AI or remote publication',
        }, null, 2));
      }
    } finally {
      fs.closeSync(log);
      fs.rmSync(work, { recursive: true, force: true });
    }
  };
  try {
    command(['manage.py', 'migrate', '--noinput']);
    command(['manage.py', 'collectstatic', '--noinput']);
    seed = JSON.parse(command([path.join(__dirname, 'seed.py')]).trim().split('\n').at(-1));
    const start = (args) => {
      const child = spawn(python, args, {
        cwd: source, env, detached: true, stdio: ['ignore', log, log],
      });
      child.on('error', (error) => { child.startupError = error; });
      children.push(child);
      return child;
    };
    start(['-m', 'gunicorn', '--config', 'python:config.gunicorn', 'config.wsgi:application']);
    start(['manage.py', 'process_jobs']);
    const boardURLs = {};
    for (const [name, fixture] of Object.entries(seed.board_sources)) {
      const port = await availablePort();
      boardURLs[name] = `http://127.0.0.1:${port}`;
      start([path.join(fixture.source, 'board.py'), '--port', String(port)]);
    }
    let ready = false;
    for (let i = 0; i < 200; i++) {
      for (const child of children) {
        if (child.startupError) throw child.startupError;
        assert.equal(child.exitCode, null, 'E2E service exited; inspect services.log');
      }
      try {
        const response = await fetch(`${baseURL}/accounts/login/`, { signal: AbortSignal.timeout(1000) });
        const boards = await Promise.all(Object.values(boardURLs).map((url) => fetch(url, { signal: AbortSignal.timeout(1000) })));
        if (response.status === 200 && boards.every((board) => board.status === 200)) { ready = true; break; }
      } catch {}
      await pause(100);
    }
    assert.ok(ready, 'E2E server did not become ready; inspect services.log');
    process.env.AIQA_E2E_BASE_URL = baseURL;
    process.env.AIQA_E2E_FIXTURE = JSON.stringify(seed);
    process.env.AIQA_E2E_BROWSER_HOME = browserHome;
    process.env.AIQA_E2E_BOARD_URLS = JSON.stringify(boardURLs);
    return cleanup;
  } catch (error) {
    await cleanup();
    throw error;
  }
};
