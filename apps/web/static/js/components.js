// 画面共通の小部品。すべて h()（textContent）で作る。
import { h } from './dom.js';
import { MISSING, briefValue } from './format.js';
import { describeError, validationLines } from './errors.js';

export const missing = (note) => h('span', { class: 'missing' }, MISSING, note ? ` ${note}` : '');

/** 値が無ければ「未記録」。 */
export function valueOrMissing(v) {
  return v === null || v === undefined || v === '' ? missing() : String(v);
}

export function badge(text, kind = 'neutral') {
  return h('span', { class: `badge badge-${kind}` }, text);
}

/** 項目名と値の一覧（dl）。値が null/空なら「未記録」。値は node でも文字列でも良い。 */
export function kv(pairs) {
  const dl = h('dl', { class: 'kv' });
  for (const [k, v] of pairs) {
    dl.append(h('dt', {}, k), h('dd', {}, v instanceof Node ? v : valueOrMissing(v)));
  }
  return dl;
}

/** 配列を ul に。空なら「未記録」。 */
export function listOrMissing(items, render = (x) => String(x)) {
  if (!Array.isArray(items) || items.length === 0) return missing();
  return h('ul', { class: 'plain' }, items.map((i) => h('li', {}, render(i))));
}

/** 形が決まっていない dict を、深さ・件数を制限して安全に見せる（textContent のみ）。 */
export function tree(value, depth = 0) {
  if (value === null || value === undefined) return missing();
  if (typeof value !== 'object') return h('span', {}, briefValue(value, 400));
  if (depth >= 3) return h('span', { class: 'muted' }, '（深い階層は省略）');
  const entries = Array.isArray(value) ? value.map((v, i) => [String(i), v]) : Object.entries(value);
  if (entries.length === 0) return h('span', { class: 'muted' }, '（空）');
  const dl = h('dl', { class: 'kv nested' });
  for (const [k, v] of entries.slice(0, 30)) dl.append(h('dt', {}, k), h('dd', {}, tree(v, depth + 1)));
  if (entries.length > 30) dl.append(h('dt', {}, '…'), h('dd', {}, `ほか ${entries.length - 30} 件`));
  return dl;
}

/** エラー表示（role=alert）。理由・対処・コード・HTTP status・相関 ID を出す。 */
export function errorBox(err, { onRetry } = {}) {
  const d = describeError(err);
  const box = h('div', { class: 'alert alert-error', role: 'alert' });
  box.append(
    h('p', { class: 'alert-title' }, d.title),
    h('p', {}, d.reason),
    h('p', {}, h('strong', {}, '対処: '), d.action),
  );
  if (d.serverMessage) box.append(h('p', { class: 'muted' }, `サーバーのメッセージ: ${d.serverMessage}`));
  const lines = validationLines(err?.detail);
  if (lines.length) box.append(h('ul', { class: 'plain muted' }, lines.map((l) => h('li', {}, l))));
  box.append(
    h('p', { class: 'muted small' }, `コード: ${d.code} / HTTP: ${d.status === 0 ? '通信失敗' : d.status} / 相関 ID: ${d.correlationId ?? MISSING}`),
  );
  if (d.retry && onRetry) box.append(h('button', { type: 'button', class: 'btn', onclick: onRetry }, '再試行'));
  return box;
}

export function loading(text = '読み込み中…') {
  return h('p', { class: 'loading', role: 'status' }, text);
}
