// シナリオ 3（詳細・時系列は未記録を捏造しない）と 4（権限はサーバーが最終判定）。
import test from 'node:test';
import assert from 'node:assert/strict';
import { skipReason, suite, newPage, openDetail, dialog, posts, until, IDS } from './harness.mjs';

const skip = (await skipReason()) ?? false;
let env;
test.before(async (t) => { if (!skip) env = await suite(t); });
test.beforeEach(async () => { if (!skip) await env.server.reset(); });

const count = (text, needle) => text.split(needle).length - 1;

test('3. 詳細: 記録が無い項目は「未記録」で、値を捏造しない', { skip }, async () => {
  const { server, browser } = env;
  const { page, problems } = await newPage(browser);
  await openDetail(page, server, IDS.draft, server.tokens.owner);
  await until(async () => (await page.locator('main').innerText()).includes('Upload'), { what: 'stages' });
  const text = await page.locator('main').innerText();
  assert.match(text, /（トピック未記録）/);
  // 7 ステージはどれも記録が無い → 各ステージが「未記録」。監査は 0 件
  for (const stage of ['Research', 'Strategy・Brief', 'Script', 'Storyboard', 'Production', 'Render', 'Upload']) {
    assert.match(text, new RegExp(`${stage}\\n未記録`), stage);
  }
  assert.match(text, /0 件\s+該当する記録はありません/);
  // LLM の費用は 0 円ではなく未計測
  assert.match(text, /LLM の費用\nunmeasured（LLM の費用は計測されていません。0 円ではありません）/);
  // 失敗が無い Episode に失敗を作らない
  assert.doesNotMatch(text, /直近の失敗/);
  assert.deepEqual(problems, []);

  // 失敗した Episode: サーバーが持つ失敗だけを出す（code は progress の値のまま）
  await page.goto(`${server.base}/ui/#/episodes/${IDS.failed}`);
  await page.waitForSelector('summary:has-text("Production")');
  await page.click('summary:has-text("Production")');
  await page.waitForSelector('text=provider_rejected');
  const failed = await page.locator('main').innerText();
  assert.match(failed, /provider_rejected/);
  assert.match(failed, /terminal_failed/);
  assert.match(failed, /要注意: 失敗/);
});

test('3b. 監査: hold の reason / correlation_id は read model の値。無ければ「未記録」', { skip }, async () => {
  const { server, browser } = env;
  const { page } = await newPage(browser);
  await openDetail(page, server, IDS.running, server.tokens.owner);
  // reason 付き
  await page.click('button:has-text("一時停止を要求")');
  await page.fill('#hold-reason', '内容を確認するため');
  await page.click('dialog[open] button:has-text("hold を要求する")');
  await page.waitForSelector('dialog[open] >> text=受け付けました');
  await page.click('dialog[open] button:has-text("閉じる")');
  await until(async () => (await page.locator('#audit').innerText()).includes('内容を確認するため'), { what: 'reason in audit' });
  const log = await server.log();
  const hold = posts(log).find((e) => e.path.endsWith('/hold'));
  const audit = await page.locator('#audit').innerText();
  assert.match(audit, /reason\n内容を確認するため/);
  assert.ok(hold.correlation_id, 'ブラウザが X-Correlation-ID を付けた');
  assert.match(audit, new RegExp(`correlation_id\\n${hold.correlation_id}`));

  // reason 無し（もう 1 回 hold。同じ revision なので受け付けられる）
  await page.click('button:has-text("一時停止を要求")');
  await page.click('dialog[open] button:has-text("hold を要求する")');
  await page.waitForSelector('dialog[open] >> text=受け付けました');
  await page.click('dialog[open] button:has-text("閉じる")');
  await until(async () => (await page.locator('#audit').innerText()).includes('0 件') === false && (await page.locator('#audit').innerText()).match(/2 件/), { what: '2 commands' });
  const audit2 = await page.locator('#audit').innerText();
  assert.match(audit2, /reason\n未記録/);
});

test('4. 権限: 読み取り専用の principal の hold / apply はサーバーが 403 で拒否し、理由を表示する', { skip }, async () => {
  const { server, browser } = env;
  const { page } = await newPage(browser);
  await openDetail(page, server, IDS.running, server.tokens.reader);
  // ボタンは隠れていない（UI は権限を判定しない）。押すとサーバーが拒否する
  await page.click('button:has-text("一時停止を要求")');
  await page.fill('#hold-reason', 'reader tries');
  await page.click('dialog[open] button:has-text("hold を要求する")');
  await page.waitForSelector('dialog[open] >> text=必要な権限（scope）がありません');
  const msg = await dialog(page).innerText();
  assert.match(msg, /missing_scope/);
  assert.match(msg, /HTTP: 403/);
  assert.match(msg, /サーバーのメッセージ: this caller lacks the 'hold' scope/);
  const log = await server.log();
  const hold = posts(log).filter((e) => e.path.endsWith('/hold'));
  assert.equal(hold.length, 1, 'サーバーに届いた');
  assert.equal(hold[0].principal, 'reader');
  assert.equal(hold[0].status, 403);
  assert.equal(hold[0].error, 'missing_scope');
  assert.deepEqual(await server.accepted(), [], 'command は受け付けられていない');
  await page.click('dialog[open] button:has-text("キャンセル")');

  // apply（変更案のある Episode）。プレビュー（read）は読めるが apply は 403
  await page.goto(`${server.base}/ui/#/episodes/${IDS.awaiting}`);
  await page.waitForSelector('text=変更案（承認待ち）');
  await page.locator('.panel', { hasText: 'cs-with-estimate' }).getByRole('button', { name: '承認して適用…' }).click();
  await page.waitForSelector('dialog[open] #apply-max');
  await page.fill('#apply-max', '2');
  await page.check('#apply-ack');
  await page.click('dialog[open] button:has-text("承認して適用する")');
  await page.waitForSelector('dialog[open] >> text=必要な権限（scope）がありません');
  const applyCalls = posts(await server.log()).filter((e) => e.path.endsWith('/apply'));
  assert.equal(applyCalls.length, 1);
  assert.equal(applyCalls[0].status, 403);
  assert.equal(applyCalls[0].error, 'missing_scope');
  assert.deepEqual(await server.accepted(), []);
});

test('4b. 権限: AI アシスタントは「制作を開始」を実行できない（サーバーが拒否）', { skip }, async () => {
  const { server, browser } = env;
  const { page } = await newPage(browser);
  await openDetail(page, server, IDS.draft, server.tokens.assistant);
  await page.click('button:has-text("制作を開始")');
  await page.check('#start-ack');
  await page.click('dialog[open] button:has-text("制作を開始する")');
  await page.waitForSelector('dialog[open] >> text=HTTP: 403');
  const call = posts(await server.log()).find((e) => e.path.endsWith('/production'));
  assert.equal(call.principal, 'assistant');
  assert.equal(call.status, 403);
});
