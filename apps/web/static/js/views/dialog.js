// 確認ダイアログ（<dialog>）。書き込み操作は必ずここを通る。
//  - command_id は開いたとき 1 回だけ作り、通信失敗・5xx の再試行では同じものを使う（冪等）。
//    サーバーが確定的に拒否した（4xx）あとの再送は新しい操作なので、新しい command_id にする。
//  - 送信中は submit を無効化（二重送信しない）。
//  - 送信直前に route の Episode と workspace の Episode が一致するか再確認する。
import { h, replaceChildren } from '../dom.js';
import { errorBox, loading } from '../components.js';
import { describeError } from '../errors.js';
import { pinControlRevision, verifyEpisode } from '../requests.js';
import { MISSING } from '../format.js';

let dialogSeq = 0;

function baseDialog(ctx, { title, episodeId }) {
  const id = `dlg-${++dialogSeq}`;
  const dlg = h('dialog', { 'aria-labelledby': `${id}-t` });
  const msg = h('div', { 'aria-live': 'polite' });
  const body = h('div', {});
  const actions = h('div', { class: 'dialog-actions' });
  dlg.append(
    h('h2', { id: `${id}-t` }, title),
    h('p', { class: 'muted' }, '対象 Episode: ', h('span', { class: 'episode-id' }, episodeId)),
    msg,
    body,
    actions,
  );
  const opener = document.activeElement;
  dlg.addEventListener('close', () => {
    dlg.remove();
    if (opener && typeof opener.focus === 'function' && document.contains(opener)) opener.focus();
  });
  document.body.append(dlg);
  return { dlg, msg, body, actions };
}

/** 読み取りだけのダイアログ。 */
export function openReadDialog(ctx, { title, episodeId, load }) {
  const { dlg, msg, body, actions } = baseDialog(ctx, { title, episodeId });
  const close = h('button', { type: 'button', class: 'btn', onclick: () => dlg.close() }, '閉じる');
  actions.append(close);
  async function run() {
    replaceChildren(msg, loading());
    try {
      replaceChildren(body, await load());
      replaceChildren(msg);
    } catch (err) {
      if (err?.name === 'AbortError') return;
      replaceChildren(msg, errorBox(err, { onRetry: run }));
    }
  }
  dlg.showModal();
  run();
  return dlg;
}

/**
 * @param cfg {title, episodeId, intro?, prepare?(body), validate(), send(commandId, correlationId, {controlRevision}),
 *             renderSuccess(data, correlationId), submitLabel, danger?}
 */
export function openOperationDialog(ctx, cfg) {
  const { dlg, msg, body, actions } = baseDialog(ctx, cfg);
  let commandId = ctx.uuid();
  const controlRevision = pinControlRevision(ctx.getWorkspace()); // 開いた時点の値。送信時に読み直さない
  let inflight = false;
  let finished = false;
  let ready = !cfg.prepare;

  if (cfg.intro) body.append(cfg.intro);
  const content = h('div', {});
  body.append(content);

  const submit = h('button', { type: 'submit', class: `btn ${cfg.danger ? 'btn-danger' : 'btn-primary'}` }, cfg.submitLabel);
  const cancel = h('button', { type: 'button', class: 'btn', onclick: () => { if (!inflight) dlg.close(); } }, 'キャンセル');
  actions.append(cancel, submit);
  const setBusy = (b) => {
    inflight = b;
    submit.disabled = b || !ready || finished;
    dlg.setAttribute('aria-busy', String(b));
  };

  dlg.addEventListener('cancel', (ev) => { if (inflight) ev.preventDefault(); });
  // 閉じた（画面遷移で閉じられた場合を含む）ダイアログは二度と送らない。保持されたボタンからの送信も止める
  dlg.addEventListener('close', () => { finished = true; });

  async function prepare() {
    ready = false;
    submit.disabled = true;
    replaceChildren(msg, loading());
    try {
      await cfg.prepare(content);
      ready = true;
      replaceChildren(msg);
    } catch (err) {
      replaceChildren(msg, errorBox(err, { onRetry: prepare }));
    }
    setBusy(false);
  }

  async function onSubmit() {
    if (inflight || finished) return;
    const mismatch = verifyEpisode(cfg.episodeId, ctx.getWorkspace());
    const invalid = mismatch ?? cfg.validate();
    if (invalid) {
      replaceChildren(msg, h('div', { class: 'alert alert-error', role: 'alert' }, invalid));
      return;
    }
    setBusy(true);
    replaceChildren(msg, loading('送信中…'));
    const correlationId = ctx.uuid();
    try {
      const { data, correlationId: echoed } = await cfg.send(commandId, correlationId, { controlRevision });
      finished = true;
      replaceChildren(body);
      replaceChildren(
        msg,
        h('div', { class: 'alert alert-ok', role: 'status' }, h('p', { class: 'alert-title' }, '受け付けました'), cfg.renderSuccess(data), h('p', { class: 'muted small' }, `相関 ID: ${echoed ?? correlationId ?? MISSING}`)),
      );
      submit.hidden = true;
      cancel.textContent = '閉じる';
      ctx.announce('操作を受け付けました');
      await ctx.reloadWorkspace();
    } catch (err) {
      if (err?.name === 'AbortError') {
        setBusy(false);
        return;
      }
      const d = describeError(err);
      const ambiguous = d.status === 0 || d.status >= 500 || d.retry;
      const box = errorBox(err);
      if (d.reload) {
        finished = true;
        submit.hidden = true;
        cancel.textContent = '閉じる';
        replaceChildren(body);
        box.append(h('p', {}, '最新の状態を読み込み直しました。このダイアログを閉じて、内容を確認してからやり直してください。'));
        replaceChildren(msg, box);
        await ctx.reloadWorkspace();
      } else {
        if (ambiguous) {
          submit.textContent = '再試行（同じ操作として送ります）';
        } else {
          commandId = ctx.uuid(); // 確定的な拒否のあとの再送は新しい操作
        }
        replaceChildren(msg, box);
      }
    } finally {
      setBusy(false);
    }
  }

  submit.addEventListener('click', (ev) => { ev.preventDefault(); onSubmit(); });
  dlg.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter' && ev.target?.tagName === 'INPUT' && !ev.isComposing) { ev.preventDefault(); onSubmit(); }
  });

  dlg.showModal();
  if (cfg.prepare) prepare();
  return dlg;
}
