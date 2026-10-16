// シナリオ 5（二重送信・再試行の command_id）、6（stale）、7（適用ダイアログ）、8（別 Episode への移動）。
import test from 'node:test';
import assert from 'node:assert/strict';
import { skipReason, suite, newPage, openDetail, dialog, posts, until, sleep, IDS } from './harness.mjs';

const skip = (await skipReason()) ?? false;
let env;
test.before(async (t) => { if (!skip) env = await suite(t); });
test.beforeEach(async () => { if (!skip) await env.server.reset(); });

const holdPosts = async (server) => posts(await server.log()).filter((e) => e.path.endsWith('/hold'));

test('5a. 二重クリックでも hold の POST はサーバーに 1 回だけ届く', { skip }, async () => {
  const { server, browser } = env;
  const { page } = await newPage(browser);
  await openDetail(page, server, IDS.running, server.tokens.owner);
  // 応答を遅らせて、1 回目が処理中のうちに 2 回目のクリックが来るようにする
  await page.route('**/hold', async (route) => { await sleep(600); await route.continue(); });
  await page.click('button:has-text("一時停止を要求")');
  await page.locator('dialog[open] button:has-text("hold を要求する")').dblclick();
  await page.waitForSelector('dialog[open] >> text=受け付けました');
  await sleep(300);
  const calls = await holdPosts(server);
  assert.equal(calls.length, 1);
  assert.equal((await server.accepted()).length, 1);
});

test('5b. 通信失敗後の再試行は同じ command_id、確定的な 4xx のあとは新しい command_id', { skip }, async () => {
  const { server, browser } = env;
  const { page } = await newPage(browser);
  await openDetail(page, server, IDS.running, server.tokens.owner);
  // 1 回目: サーバーは処理するが、応答がブラウザに届かない（最悪のケース）
  let first = true;
  await page.route('**/hold', async (route) => {
    if (first) {
      first = false;
      await route.fetch();
      await route.abort('failed');
    } else await route.continue();
  });
  await page.click('button:has-text("一時停止を要求")');
  await page.fill('#hold-reason', '再試行の確認');
  await page.click('dialog[open] button:has-text("hold を要求する")');
  await page.waitForSelector('dialog[open] >> text=サーバーに接続できませんでした');
  assert.match(await dialog(page).innerText(), /通信失敗/);
  await page.click('dialog[open] button:has-text("再試行（同じ操作として送ります）")');
  await page.waitForSelector('dialog[open] >> text=同じ内容の再送');
  const calls = await holdPosts(server);
  assert.equal(calls.length, 2);
  assert.equal(calls[0].command_id, calls[1].command_id, '同じ command_id');
  assert.equal(calls[0].status, 202);
  assert.equal(calls[1].status, 200, 'サーバーは再送として扱う');
  assert.equal((await server.accepted()).length, 1, 'command は 1 件だけ');
});

test('5c. 確定的な拒否（403）のあとに再送すると、新しい command_id になる', { skip }, async () => {
  const { server, browser } = env;
  const { page } = await newPage(browser);
  await openDetail(page, server, IDS.running, server.tokens.reader);
  await page.click('button:has-text("一時停止を要求")');
  await page.click('dialog[open] button:has-text("hold を要求する")');
  await page.waitForSelector('dialog[open] >> text=HTTP: 403');
  await page.click('dialog[open] button:has-text("hold を要求する")');
  await until(async () => (await holdPosts(server)).length === 2, { what: 'second post' });
  const calls = await holdPosts(server);
  assert.notEqual(calls[0].command_id, calls[1].command_id);
  assert.deepEqual(calls.map((c) => c.status), [403, 403]);
});

test('6. stale_control_revision: 409 を説明し、最新を読み直す', { skip }, async () => {
  const { server, browser } = env;
  const { page } = await newPage(browser);
  await openDetail(page, server, IDS.awaiting, server.tokens.owner);
  assert.match(await page.locator('main').innerText(), /revision 4/);
  await page.click('button:has-text("一時停止を要求")');
  await server.bump(IDS.awaiting); // 別の操作者が先に制御状態を進めた
  await server.clearLog();
  await page.click('dialog[open] button:has-text("hold を要求する")');
  await page.waitForSelector('dialog[open] >> text=最新の状態を読み込み直しました');
  const text = await dialog(page).innerText();
  assert.match(text, /画面を開いた後に、他の操作で Episode の制御状態が変わりました/);
  assert.match(text, /stale_control_revision/);
  const log = await server.log();
  const hold = log.find((e) => e.path.endsWith('/hold'));
  assert.equal(hold.status, 409);
  assert.ok(log.some((e) => e.method === 'GET' && e.path.endsWith('/workspace')), 'workspace を読み直した');
  assert.equal((await server.accepted()).length, 0);
  // 再送ボタンは無く、閉じるだけ（古い revision で送り直さない）
  assert.equal(await page.locator('dialog[open] button:has-text("hold を要求する")').isVisible(), false);
  await page.click('dialog[open] button:has-text("閉じる")');
  await until(async () => /revision 5/.test(await page.locator('main').innerText()), { what: 'revision 5 shown' });
  // 読み直した後なら受け付けられる
  await page.click('button:has-text("一時停止を要求")');
  await page.click('dialog[open] button:has-text("hold を要求する")');
  await page.waitForSelector('dialog[open] >> text=受け付けました');
  assert.equal((await server.accepted()).length, 1);
});

async function openApply(page, csId) {
  await page.locator('.panel', { hasText: csId }).getByRole('button', { name: '承認して適用…' }).click();
  await page.waitForSelector('dialog[open] #apply-max');
}
const applyPosts = async (server) => posts(await server.log()).filter((e) => e.path.endsWith('/apply'));

test('7a. 適用: 見積り・blocker を見せ、上限額が空/不足/未確認なら送らず、満たすと 1 回だけ送る', { skip }, async () => {
  const { server, browser } = env;
  const { page } = await newPage(browser);
  await openDetail(page, server, IDS.awaiting, server.tokens.owner);
  await openApply(page, 'cs-with-estimate');
  const text = await dialog(page).innerText();
  assert.match(text, /費用の見積り\n\$1\.25/);
  assert.match(text, /適用できない理由 \(blockers\)\nなし/);
  assert.equal(await page.inputValue('#apply-max'), '', '上限額に既定値は入らない');
  const submit = 'dialog[open] button:has-text("承認して適用する")';
  await page.click(submit);
  await page.waitForSelector('dialog[open] >> text=承認する上限額（USD）を入力してください');
  await page.fill('#apply-max', '1');
  await page.click(submit);
  await page.waitForSelector('dialog[open] >> text=上限額は見積り');
  await page.fill('#apply-max', '2');
  await page.click(submit);
  await page.waitForSelector('dialog[open] >> text=確認にチェックしてください');
  assert.equal((await applyPosts(server)).length, 0, '検査を通るまで 1 件も送っていない');
  await page.check('#apply-ack');
  await page.click(submit);
  await page.waitForSelector('dialog[open] >> text=受け付けました');
  const calls = await applyPosts(server);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].status, 202);
  assert.deepEqual(calls[0].body_keys, ['approved_max_cost_usd', 'command_id', 'expected_control_revision']);
  assert.match(await dialog(page).innerText(), /完了ではありません/);
});

test('7b. 適用: 見積りが null の変更は画面でも送らず、サーバーも拒否する', { skip }, async () => {
  const { server, browser } = env;
  const { page } = await newPage(browser);
  await openDetail(page, server, IDS.awaiting, server.tokens.owner);
  await openApply(page, 'cs-no-estimate');
  const text = await dialog(page).innerText();
  assert.match(text, /費用の見積り\n未見積り（0 円ではありません）/);
  assert.match(text, /cost_not_estimated/);
  await page.fill('#apply-max', '100');
  await page.check('#apply-ack');
  await page.click('dialog[open] button:has-text("承認して適用する")');
  await page.waitForSelector('dialog[open] >> text=見積もられていないため適用できません');
  assert.equal((await applyPosts(server)).length, 0);
  // 画面を迂回してサーバーに直接送っても、サーバー（本物の ApplicationService）が拒否する
  const r = await fetch(`${server.base}/v1/intervention/episodes/${IDS.awaiting}/change-sets/cs-no-estimate/apply`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${server.tokens.owner}`, 'Content-Type': 'application/json' },
    body: JSON.stringify({ command_id: 'direct-bypass-1', expected_control_revision: 4, approved_max_cost_usd: 100 }),
  });
  assert.equal(r.status, 422);
  assert.equal((await r.json()).detail.error, 'cost_not_estimated');
  assert.deepEqual(await server.accepted(), []);
});

test('8. 別 Episode へ移動するとダイアログは閉じ、送信は起きない', { skip }, async () => {
  const { server, browser } = env;
  const { page } = await newPage(browser);
  await openDetail(page, server, IDS.running, server.tokens.owner);
  await page.click('button:has-text("一時停止を要求")');
  assert.match(await dialog(page).innerText(), new RegExp(IDS.running));
  await page.evaluate((id) => { location.hash = `#/episodes/${id}`; }, IDS.awaiting);
  await page.waitForSelector('text=承認待ちの回');
  assert.equal(await dialog(page).count(), 0, 'ダイアログは閉じている');
  // 画面を離れても Enter / 保持されたボタンで送れない
  await page.keyboard.press('Enter');
  await sleep(300);
  assert.equal((await holdPosts(server)).length, 0);
  // 新しい Episode の画面で開き直したダイアログは、新しい Episode を対象にする
  await page.click('button:has-text("一時停止を要求")');
  const t = await dialog(page).innerText();
  assert.match(t, new RegExp(IDS.awaiting));
  assert.doesNotMatch(t, new RegExp(IDS.running));
  await page.click('dialog[open] button:has-text("hold を要求する")');
  await page.waitForSelector('dialog[open] >> text=受け付けました');
  const calls = await holdPosts(server);
  assert.equal(calls.length, 1);
  assert.ok(calls[0].path.includes(IDS.awaiting));
});

test('8b. 閉じたダイアログの送信ボタンを（保持して）後から押しても何も送られない', { skip }, async () => {
  const { server, browser } = env;
  const { page } = await newPage(browser);
  await openDetail(page, server, IDS.running, server.tokens.owner);
  await page.click('button:has-text("一時停止を要求")');
  await page.evaluate(() => { window.__held = document.querySelector('dialog[open] button.btn-primary'); });
  await page.evaluate((id) => { location.hash = `#/episodes/${id}`; }, IDS.awaiting);
  await page.waitForSelector('text=承認待ちの回');
  await page.evaluate(() => window.__held.click());
  await sleep(400);
  assert.equal((await holdPosts(server)).length, 0);
});
