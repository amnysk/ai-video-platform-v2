// ブラウザ E2E の共通部品。本物の FastAPI app（tests/e2e_ui/server.py、fake port 付き）を子プロセスで起動し、
// headless Chrome を playwright-core で動かす。Chrome / node_modules が無ければ呼び出し側が skip する。
import { spawn } from 'node:child_process';
import { existsSync, readdirSync } from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { fileURLToPath } from 'node:url';
import readline from 'node:readline';

const HERE = path.dirname(fileURLToPath(import.meta.url));
export const ROOT = path.resolve(HERE, '..', '..');

export function findChrome() {
  const env = process.env.CHROME_PATH;
  if (env && existsSync(env)) return env;
  for (const p of ['/usr/bin/google-chrome', '/usr/bin/google-chrome-stable', '/usr/bin/chromium', '/usr/bin/chromium-browser']) {
    if (existsSync(p)) return p;
  }
  const cache = path.join(os.homedir(), '.cache', 'ms-playwright');
  if (existsSync(cache)) {
    for (const d of readdirSync(cache).filter((x) => x.startsWith('chromium-')).sort().reverse()) {
      for (const sub of ['chrome-linux64/chrome', 'chrome-linux/chrome']) {
        const p = path.join(cache, d, sub);
        if (existsSync(p)) return p;
      }
    }
  }
  return null;
}

export async function loadPlaywright() {
  try {
    return await import('playwright-core');
  } catch {
    return null;
  }
}

/** skip 理由。null なら実行できる。 */
export async function skipReason() {
  if (!findChrome()) return 'Chrome/Chromium が見つかりません';
  if (!(await loadPlaywright())) return 'playwright-core が未インストールです（tests/e2e_ui で npm ci）';
  const py = pythonPath();
  if (!py) return 'Python の仮想環境が見つかりません';
  return null;
}

function pythonPath() {
  if (process.env.E2E_PYTHON) return process.env.E2E_PYTHON;
  const venv = path.join(ROOT, '.venv', 'bin', 'python');
  return existsSync(venv) ? venv : 'python3';
}

export async function startServer({ flagOff = false } = {}) {
  const args = ['-m', 'tests.e2e_ui.server', ...(flagOff ? ['--flag-off'] : [])];
  const child = spawn(pythonPath(), args, { cwd: ROOT, stdio: ['pipe', 'pipe', 'inherit'], env: { ...process.env, PYTHONPATH: ROOT } });
  const rl = readline.createInterface({ input: child.stdout });
  const info = await new Promise((resolve, reject) => {
    const t = setTimeout(() => reject(new Error('server start timeout')), 30000);
    rl.once('line', (line) => { clearTimeout(t); resolve(JSON.parse(line)); });
    child.once('exit', (c) => reject(new Error(`server exited early: ${c}`)));
  });
  const base = `http://127.0.0.1:${info.port}`;
  const admin = async (method, p) => {
    const r = await fetch(base + p, { method });
    return r.json();
  };
  return {
    base,
    tokens: info.tokens,
    log: () => admin('GET', '/__e2e/log'),
    accepted: () => admin('GET', '/__e2e/accepted'),
    reset: () => admin('POST', '/__e2e/reset'),
    clearLog: () => admin('POST', '/__e2e/clear-log'),
    bump: (episodeId) => admin('POST', `/__e2e/bump/${episodeId}`),
    async stop() {
      child.stdin.end();
      await new Promise((r) => { child.once('exit', r); setTimeout(() => { child.kill(); r(); }, 5000); });
    },
  };
}

export async function launchBrowser() {
  const { chromium } = await loadPlaywright();
  return chromium.launch({ executablePath: findChrome(), headless: true, args: ['--no-sandbox'] });
}

/** 新しい context + page。console の error/warning と CSP 違反を集める。 */
export async function newPage(browser, { clock = false } = {}) {
  const context = await browser.newContext({ viewport: { width: 1100, height: 900 }, locale: 'ja-JP' });
  const page = await context.newPage();
  const problems = [];
  page.on('console', (m) => {
    if (['error', 'warning'].includes(m.type())) problems.push(`console.${m.type()}: ${m.text()}`);
  });
  page.on('pageerror', (e) => problems.push(`pageerror: ${e.message}`));
  await page.addInitScript(() => {
    window.__csp = [];
    document.addEventListener('securitypolicyviolation', (e) => {
      window.__csp.push(`${e.violatedDirective} ${e.blockedURI}`);
    });
  });
  if (clock) await page.clock.install();
  return { context, page, problems, csp: () => page.evaluate(() => window.__csp) };
}

export async function login(page, base, token) {
  await page.goto(`${base}/ui/`);
  await page.fill('#token', token);
  await page.click('button[type=submit]');
}

export const textOf = (page, sel = 'body') => page.locator(sel).innerText();

export const IDS = {
  draft: '00000000-0000-4000-8000-0000000000d1',
  running: '00000000-0000-4000-8000-0000000000a2',
  failed: '00000000-0000-4000-8000-0000000000f3',
  awaiting: '00000000-0000-4000-8000-0000000000c4',
};

/** 1 ファイル = 1 server + 1 browser。test ごとに world を reset する。 */
export async function suite(t, fn, opts) {
  const server = await startServer(opts);
  const browser = await launchBrowser();
  t.after(async () => { await browser.close(); await server.stop(); });
  return { server, browser };
}

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export function detailUrl(base, id) {
  return `${base}/ui/#/episodes/${id}`;
}

/** ログイン済みで詳細を開く */
export async function openDetail(page, server, id, token) {
  await login(page, server.base, token);
  await page.waitForSelector('table');
  await page.goto(detailUrl(server.base, id));
  await page.waitForSelector('text=介入操作');
}

export const dialog = (page) => page.locator('dialog[open]');
export const posts = (log) => log.filter((e) => e.method === 'POST' && e.path.startsWith('/v1'));

/** waitForFunction は eval を使うため、CSP（unsafe-eval 無し）の下では使えない。node 側でポーリングする。 */
export async function until(fn, { timeout = 8000, what = 'condition' } = {}) {
  const t0 = Date.now();
  let last;
  while (Date.now() - t0 < timeout) {
    try {
      last = await fn();
      if (last) return last;
    } catch (e) { last = e; }
    await sleep(40);
  }
  throw new Error(`timeout waiting for ${what} (last: ${last instanceof Error ? last.message : last})`);
}
