import { h } from '../dom.js';

/** トークン入力。sessionStorage にだけ保存する（タブを閉じると消える）。 */
export function loginView(ctx, { message } = {}) {
  const input = h('input', { type: 'password', id: 'token', name: 'token', autocomplete: 'off', required: true, spellcheck: 'false' });
  const form = h(
    'form',
    {
      onsubmit: (ev) => {
        ev.preventDefault();
        if (ctx.session.setToken(input.value)) {
          input.value = '';
          ctx.rerender();
        }
      },
    },
    h('label', { for: 'token' }, 'Bearer トークン', h('span', { class: 'field-hint' }, ' （API の principal のトークン）')),
    input,
    h('p', { class: 'actions' }, h('button', { type: 'submit', class: 'btn btn-primary' }, 'ログイン')),
  );
  const el = h(
    'section',
    {},
    h('h2', {}, 'ログイン'),
    message ? h('div', { class: 'alert alert-warn', role: 'alert' }, message) : null,
    form,
    h('p', { class: 'muted small' }, 'トークンはこのタブの sessionStorage にだけ保存され、タブを閉じると消えます。画面や URL には表示しません。権限の判定はサーバーが行います。'),
  );
  queueMicrotask(() => input.focus());
  return { el, dispose() {} };
}
