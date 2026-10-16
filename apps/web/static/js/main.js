import { createSession } from './state/session.js';
import { createApi } from './api.js';
import { loadOperations } from './operations.js';
import { parseHash } from './router.js';
import { h, replaceChildren } from './dom.js';
import { createRenderGate } from './render_gate.js';
import { loginView } from './views/login.js';
import { listView } from './views/list.js';
import { detailView } from './views/detail.js';

const main = document.getElementById('main');
const live = document.getElementById('live');
const logout = document.getElementById('logout');

const session = createSession();
let unauthorized = false;
const api = createApi({
  session,
  onUnauthorized: () => {
    unauthorized = true; // 401: トークンは api が破棄済み。ログイン画面へ
    queueMicrotask(render);
  },
});
let ops; // undefined = 未読込, null = 読めない
const gate = createRenderGate(); // await をまたぐ render の世代管理（古い render は捨てる・view を漏らさない）

const ctx = {
  api,
  session,
  uuid: () => crypto.randomUUID(),
  announce(text) {
    live.textContent = '';
    setTimeout(() => { live.textContent = text; }, 50);
  },
  rerender: () => render(),
};

logout.addEventListener('click', () => {
  session.clear();
  render();
});

async function render() {
  const token = gate.begin();
  for (const d of document.querySelectorAll('dialog[open]')) d.close(); // 画面を離れたら確認ダイアログも閉じる
  gate.disposeCurrent();
  logout.hidden = !session.hasToken();

  if (!session.hasToken()) {
    const message = unauthorized ? '認証に失敗しました。トークンを入力し直してください。' : undefined;
    unauthorized = false;
    const view = loginView(ctx, { message });
    if (!gate.install(token, view)) return;
    replaceChildren(main, view.el);
    return;
  }
  if (ops === undefined || ops === null) {
    const loaded = await loadOperations((...a) => fetch(...a));
    if (!token.isCurrent()) return; // 待っている間に別の render が始まった
    ops = loaded;
  }
  if (ops === null) {
    ops = undefined;
    replaceChildren(
      main,
      h('div', { class: 'alert alert-error', role: 'alert' },
        h('p', { class: 'alert-title' }, '操作カタログ (operations.json) を読み込めません'),
        h('p', {}, 'API の操作一覧が無いため、画面を表示できません。サーバーの配信状態を確認してください。'),
        h('button', { type: 'button', class: 'btn', onclick: () => render() }, '再試行')),
    );
    return;
  }
  const route = parseHash(location.hash);
  const rctx = { ...ctx, ops };
  let view;
  if (route.name === 'list') view = listView(rctx);
  else if (route.name === 'detail') view = detailView(rctx, route.episodeId);
  else view = { el: h('div', {}, h('h2', {}, 'ページが見つかりません'), h('a', { href: '#/' }, '一覧へ')), dispose() {} };
  if (!gate.install(token, view)) return;
  replaceChildren(main, view.el);
  main.focus({ preventScroll: true });
}

window.addEventListener('hashchange', render);
render();
