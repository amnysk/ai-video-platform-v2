// シナリオ 9（flag OFF）、10（CSP・セキュリティヘッダ）、11（polling）、および /ui → /ui/。
import test from 'node:test';
import assert from 'node:assert/strict';
import { skipReason, suite, newPage, login, openDetail, dialog, until, startServer, IDS } from './harness.mjs';

const skip = (await skipReason()) ?? false;
let env;
test.before(async (t) => { if (!skip) env = await suite(t); });
test.beforeEach(async () => { if (!skip) await env.server.reset(); });

const HEADERS = {
  'content-security-policy': "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
  'x-content-type-options': 'nosniff',
  'referrer-policy': 'no-referrer',
  'cache-control': 'no-store',
};

test('9. flag OFF: /ui/ も /v1 も 404 feature_disabled で、UI の資産は一切配信されない', { skip }, async (t) => {
  const off = await startServer({ flagOff: true });
  t.after(() => off.stop());
  for (const p of ['/ui/', '/ui', '/ui/index.html', '/ui/js/main.js', '/ui/app.css', '/ui/operations.json', '/ui/favicon.svg']) {
    const r = await fetch(off.base + p, { redirect: 'manual' });
    assert.equal(r.status, 404, p);
    const body = await r.json();
    assert.equal(body.detail.error, 'feature_disabled', p);
  }
  for (const p of ['/v1/intervention/episodes', `/v1/intervention/episodes/${IDS.draft}/workspace`]) {
    const r = await fetch(off.base + p, { headers: { Authorization: `Bearer ${off.tokens.owner}` } });
    assert.equal(r.status, 404, p);
    assert.equal((await r.json()).detail.error, 'feature_disabled', p);
  }
  const post = await fetch(`${off.base}/v1/intervention/episodes/${IDS.running}/hold`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${off.tokens.owner}`, 'Content-Type': 'application/json' },
    body: JSON.stringify({ command_id: 'off-hold-0001' }),
  });
  assert.equal(post.status, 404);
  assert.deepEqual(await off.accepted(), []);
  // ブラウザで開いても、アプリは出ない
  const { page } = await newPage(env.browser);
  await page.goto(`${off.base}/ui/`);
  const text = await page.locator('body').innerText();
  assert.match(text, /feature_disabled/);
  assert.doesNotMatch(text, /運用者コンソール/);
  assert.equal(await page.locator('#token').count(), 0);
});

test('10. CSP 違反が全画面で 0 件。セキュリティヘッダが付き、inline script は実際に遮断される', { skip }, async () => {
  const { server, browser } = env;
  // ヘッダ（HTML・JS・CSS・JSON・アイコン）
  for (const p of ['/ui/', '/ui/js/main.js', '/ui/app.css', '/ui/operations.json', '/ui/favicon.svg', '/ui/nope.js']) {
    const r = await fetch(server.base + p);
    for (const [k, v] of Object.entries(HEADERS)) assert.equal(r.headers.get(k), v, `${p} ${k}`);
  }
  const { page, problems, csp } = await newPage(browser);
  // /ui → /ui/
  await page.goto(`${server.base}/ui`);
  assert.equal(new URL(page.url()).pathname, '/ui/');
  await page.waitForSelector('#token'); // ログイン画面
  await login(page, server.base, server.tokens.owner);
  await page.waitForSelector('table');
  await page.goto(`${server.base}/ui/#/episodes/${IDS.awaiting}`);
  await page.waitForSelector('text=変更案（承認待ち）');
  // 開ける画面・ダイアログを一通り
  for (const [button, waitFor] of [
    ['一時停止を要求', 'dialog[open] #hold-reason'],
    ['絵コンテの変更を提案', 'dialog[open] #prop-text'],
    ['絵コンテを見る', 'dialog[open] >> text=visual_description'],
    ['品質レビューを見る', 'dialog[open] >> text=未レビュー'],
    ['制作を開始', 'dialog[open] #start-ack'],
  ]) {
    await page.click(`button:has-text("${button}")`);
    await page.waitForSelector(waitFor);
    await page.keyboard.press('Escape');
    await until(async () => (await dialog(page).count()) === 0, { what: 'dialog closed' });
  }
  await page.locator('.panel', { hasText: 'cs-with-estimate' }).getByRole('button', { name: '影響を見る' }).click();
  await page.waitForSelector('dialog[open] >> text=影響範囲 (impact)');
  await page.keyboard.press('Escape');
  await page.locator('.panel', { hasText: 'cs-with-estimate' }).getByRole('button', { name: '承認して適用…' }).click();
  await page.waitForSelector('dialog[open] #apply-max');
  await page.keyboard.press('Escape');
  await page.click('summary:has-text("Production")');
  await page.goto(`${server.base}/ui/#/episodes/${IDS.failed}`);
  await page.waitForSelector('text=provider_rejected', { state: 'attached' });
  await page.goto(`${server.base}/ui/#/nowhere`);
  await page.waitForSelector('text=ページが見つかりません');
  assert.deepEqual(await csp(), []);
  assert.deepEqual(problems, []);

  // 対照: CSP が実際に効いている（inline script は遮断され、違反が報告される）
  await page.evaluate(() => {
    const s = document.createElement('script');
    s.textContent = 'window.__pwned = 1';
    document.head.append(s);
  });
  assert.equal(await page.evaluate(() => window.__pwned), undefined);
  assert.ok((await csp()).some((v) => v.startsWith('script-src')), '違反が検知できる');
});

test('11. polling: progress だけを読み、タブが隠れる・画面を離れると止まる', { skip }, async () => {
  const { server, browser } = env;
  const { page } = await newPage(browser, { clock: true });
  await openDetail(page, server, IDS.running, server.tokens.owner);
  await page.waitForSelector('text=/最終取得: \\d+:\\d+:\\d+ JST/');
  await sleepReal(300);
  const settle = async () => { await sleepReal(400); };
  const calls = async () => (await server.log()).map((e) => `${e.method} ${e.path.replace(IDS.running, ':id')}`);
  await server.clearLog();

  // 前面: 10 秒ごとに progress だけ
  await page.clock.runFor(10_000);
  await until(async () => (await calls()).length >= 1, { what: 'one poll' });
  await settle();
  assert.deepEqual(await calls(), ['GET /v1/intervention/episodes/:id/progress']);
  await page.clock.runFor(20_000);
  await settle();
  const after = await calls();
  assert.ok(after.length >= 2 && after.every((c) => c === 'GET /v1/intervention/episodes/:id/progress'), after.join('|'));

  // 非表示: 止まる
  await page.evaluate(() => { Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' }); });
  await server.clearLog();
  await page.clock.runFor(60_000);
  await settle();
  assert.deepEqual(await calls(), [], '隠れている間は読まない');

  // 前面に戻ると 1 回読む
  await page.evaluate(() => {
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'visible' });
    document.dispatchEvent(new Event('visibilitychange'));
  });
  await until(async () => (await calls()).length === 1, { what: 'poke on visible' });

  // 画面を離れる: 一覧へ。以後 progress は読まれない
  await page.evaluate(() => { location.hash = '#/'; });
  await page.waitForSelector('table');
  await settle();
  await server.clearLog();
  await page.clock.runFor(60_000);
  await settle();
  assert.deepEqual(await calls(), [], '画面を離れたら polling は止まる');
});

const sleepReal = (ms) => new Promise((r) => setTimeout(r, ms));

test('13. 遅い operations.json の間に画面が変わっても、古い render は view を作らない（workspace 取得は 1 回）', { skip }, async () => {
  // main.js の `if (!token.isCurrent()) return;` を外すと、遅れて戻った render が hash を読み直して
  // もう 1 つ detailView を作り、workspace を 2 回読む。
  const { server, browser } = env;
  const { page } = await newPage(browser);
  await login(page, server.base, server.tokens.owner);
  await page.waitForSelector('table');
  let calls = 0;
  await page.route('**/operations.json', async (route) => {
    calls += 1;
    if (calls === 1) await sleepReal(1500); // 1 回目だけ遅い
    await route.continue();
  });
  await server.clearLog();
  await page.goto(`${server.base}/ui/`, { waitUntil: 'domcontentloaded' });
  await page.evaluate((id) => { location.hash = `#/episodes/${id}`; }, IDS.running);
  await page.waitForSelector('text=介入操作');
  await sleepReal(2200); // 遅かった 1 回目が戻るのを待つ
  const wsReads = (await server.log()).filter((e) => e.method === 'GET' && e.path.endsWith('/workspace'));
  assert.equal(calls, 2, 'operations.json は 2 回要求された（render が 2 回走った）');
  assert.equal(wsReads.length, 1, '古い render は detailView を作らない');
});
