// シナリオ 1（一覧・絞り込み・検索・ページング）と 2（401 → ログイン、トークンの置き場）。
import test from 'node:test';
import assert from 'node:assert/strict';
import { skipReason, suite, newPage, login, until } from './harness.mjs';

const skip = (await skipReason()) ?? false;
let env;
test.before(async (t) => { if (!skip) env = await suite(t); });
test.beforeEach(async () => { if (!skip) await env.server.reset(); });

async function rows(page) {
  return page.$$eval('tbody tr td:first-child .episode-id', (els) => els.map((e) => e.textContent));
}
async function loadAll(page) {
  for (let i = 0; i < 10; i++) {
    const more = page.getByRole('button', { name: 'さらに読み込む' });
    if (!(await more.count())) return;
    const before = (await rows(page)).length;
    await more.click();
    await until(async () => (await rows(page)).length > before, { what: 'next page' });
  }
}

test('1. 一覧: 描画・状態絞り込み・検索・同一時刻のページングで重複も欠落も出ない', { skip }, async () => {
  const { server, browser } = env;
  const { page, problems } = await newPage(browser);
  await login(page, server.base, server.tokens.owner);
  await page.waitForSelector('table');
  assert.match(await page.locator('caption').innerText(), /50 件を表示中/);
  const body = await page.locator('main').innerText();
  for (const t of ['承認待ちの回', '失敗した回', '縄文の火おこし', '要注意: 失敗', '（トピック未記録）']) {
    assert.ok(body.includes(t), t);
  }

  // ページング: 129 件（>120 件が同一 created_at）。最後まで辿って重複・欠落が無い
  await loadAll(page);
  const ids = await rows(page);
  assert.equal(ids.length, 129);
  assert.equal(new Set(ids).size, 129, '重複なし');
  const topics = await page.$$eval('tbody tr td:first-child a', (els) => els.map((e) => e.textContent));
  for (let i = 0; i < 125; i++) assert.ok(topics.includes(`paging-${String(i).padStart(3, '0')}`), `paging-${i} が欠落`);
  assert.match(await page.locator('main').innerText(), /これ以上はありません/);
  const log = await server.log();
  const listCalls = log.filter((e) => e.path === '/v1/intervention/episodes');
  assert.equal(listCalls.length, 3, '50 + 50 + 29');
  assert.ok(listCalls.slice(1).every((e) => e.query.includes('before_id=')), '2 ページ目以降は複合 cursor');

  // 状態絞り込み（サーバー側）
  await page.selectOption('#status', 'failed');
  await page.click('button:has-text("検索")');
  await until(async () => (await rows(page)).length === 1, { what: '1 row' });
  assert.deepEqual(await page.$$eval('tbody tr td:first-child a', (e) => e.map((x) => x.textContent)), ['失敗した回']);
  assert.ok((await server.log()).some((e) => e.query.includes('status=failed')));

  // 検索（q）。paging-01x は 10 件
  await page.selectOption('#status', '');
  await page.fill('#q', 'paging-01');
  await page.click('button:has-text("検索")');
  await until(async () => (await rows(page)).length === 10, { what: '10 rows' });
  // 0 件
  await page.fill('#q', '存在しない題材');
  await page.click('button:has-text("検索")');
  await page.waitForSelector('text=該当する Episode はありません');
  assert.deepEqual(problems, []);
});

test('2. 401 → ログイン画面。トークンは sessionStorage だけで、localStorage・cookie・URL・DOM に出ない', { skip }, async () => {
  const { server, browser } = env;
  const { page, context } = await newPage(browser);
  await page.goto(`${server.base}/ui/`);
  await page.waitForSelector('#token');
  // 誤ったトークン → サーバーが 401 → 破棄してログインに戻る
  await page.fill('#token', 'wrong-token-value');
  await page.click('button[type=submit]');
  await page.waitForSelector('text=認証に失敗しました');
  await page.waitForSelector('#token');
  assert.equal(await page.evaluate(() => sessionStorage.length), 0, '401 でトークンを破棄');
  assert.ok((await server.log()).some((e) => e.path === '/v1/intervention/episodes' && e.status === 401));

  // 正しいトークン
  const token = server.tokens.owner;
  await page.fill('#token', token);
  await page.click('button[type=submit]');
  await page.waitForSelector('table');
  const stored = await page.evaluate(() => ({
    session: Object.values(Object.fromEntries(Object.entries(sessionStorage))),
    localLen: localStorage.length,
    cookie: document.cookie,
    url: location.href,
    html: document.documentElement.outerHTML,
  }));
  assert.deepEqual(stored.session, [token], 'sessionStorage にだけある');
  assert.equal(stored.localLen, 0);
  assert.equal(stored.cookie, '');
  assert.deepEqual(await context.cookies(), []);
  assert.ok(!stored.url.includes(token));
  assert.ok(!stored.html.includes(token));

  // 「トークンを破棄」
  await page.click('#logout');
  await page.waitForSelector('#token');
  assert.equal(await page.evaluate(() => sessionStorage.length), 0);

  // 無資格のまま API を直接叩いても 401（画面の外でも守られている）
  const r = await fetch(`${server.base}/v1/intervention/episodes`);
  assert.equal(r.status, 401);
});
