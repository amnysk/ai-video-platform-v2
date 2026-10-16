// UI-C: 介入操作。ボタンの出し分けと各ダイアログの内容。
// 操作の名前・method・path・scope は operations.json（カタログ）から取る。UI だけの操作は作らない。
import { h } from '../dom.js';
import { badge, kv, listOrMissing, tree, valueOrMissing } from '../components.js';
import { openOperationDialog, openReadDialog } from './dialog.js';
import { buildPath, availability } from '../operations.js';
import { describeReason } from '../errors.js';
import { COMMAND_STATUS_LABEL, WORKER_CHECK_LABEL, labelOf } from '../status.js';
import { COST_NOT_ESTIMATED, MISSING, formatUsd } from '../format.js';
import {
  LEGACY_START_STAGES, buildApplyBody, buildHoldBody, buildProposeBody, buildResumeBody,
  validateApply, validateHold, validateProposal, HOLD_REASON_MAX, VISUAL_DESCRIPTION_MAX,
} from '../requests.js';

function field(labelText, control, hint) {
  const id = control.id;
  return h('div', {}, h('label', { for: id }, labelText, hint ? h('span', { class: 'field-hint' }, ` ${hint}`) : null), control);
}

function acceptedView(data) {
  return kv([
    ['command_id', data.command_id],
    ['状態', `${labelOf(COMMAND_STATUS_LABEL, data.status)}（${data.status}）`],
    ['新規受付', data.created === false ? '同じ内容の再送（新しい command は作っていません）' : '新規'],
    ['注意', '「受付済み」は完了ではありません。完了は「監査・履歴」の command の状態で確認してください。'],
  ]);
}

export function previewNodes(p) {
  return h(
    'div',
    {},
    kv([
      ['変更案', p.change_set_id],
      ['種類', p.kind],
      ['状態', p.status],
      ['基準 revision', p.base_revision_id],
      ['費用の見積り', p.estimated_cost_usd === null || p.estimated_cost_usd === undefined ? COST_NOT_ESTIMATED : formatUsd(p.estimated_cost_usd)],
      ['見積り済みか', p.cost_estimated ? 'はい' : 'いいえ（未見積り）'],
      ['制御 revision', p.control_revision],
      [
        '適用できない理由 (blockers)',
        p.blockers && p.blockers.length
          ? listOrMissing(p.blockers, (b) => {
              const info = describeReason(b);
              return info ? `${b}: ${info.reason}` : b;
            })
          : 'なし',
      ],
    ]),
    h('h4', {}, '影響範囲 (impact)'),
    p.impact ? tree(p.impact) : valueOrMissing(null),
    h('h4', {}, '提案内容'),
    tree(p.proposal),
  );
}

export function interventionBar(dctx, getWorkspace) {
  const { ops, episodeId } = dctx;
  const bar = h('div', { class: 'actions', role: 'group', 'aria-label': '介入操作' });
  const hints = h('ul', { class: 'plain muted small' });

  function opButton(name, labelOrFn, onClick, { extraDisabled = null, danger = false } = {}) {
    const spec = ops.byName.get(name);
    const label = typeof labelOrFn === 'function' ? labelOrFn(spec) : labelOrFn;
    let disabledReason = extraDisabled;
    if (!spec) disabledReason = '操作カタログに無い操作です';
    else {
      const a = availability(spec, dctx.knownScopes);
      if (!a.enabled) disabledReason = a.reason;
    }
    const btn = h('button', { type: 'button', class: `btn${danger ? ' btn-danger' : ''}`, onclick: () => onClick(spec) }, label);
    if (disabledReason) {
      btn.setAttribute('aria-disabled', 'true');
      btn.disabled = true;
      btn.title = disabledReason;
    }
    if (spec) {
      hints.append(h('li', {}, `${label}: 必要な scope「${spec.scope ?? MISSING}」${spec.humanOnly ? ' / 人だけが実行できます' : ''}${spec.paid ? ' / 有料（外部生成の費用が発生しうる）' : ''}`));
    }
    bar.append(btn);
    return btn;
  }

  const ws = () => getWorkspace();

  opButton('request_hold', '一時停止を要求（hold）', (spec) => holdDialog(dctx, spec));
  opButton('resume_production', '再開（resume）', (spec) => resumeDialog(dctx, spec, ws()));
  opButton('propose_storyboard_change', '絵コンテの変更を提案', (spec) => proposeDialog(dctx, spec, ws()));
  opButton('start_agentic_production', (spec) => `制作を開始${spec?.paid ? '（有料' : '（'}${spec?.humanOnly ? '・人のみ）' : '）'}`, (spec) => startDialog(dctx, spec, ws()), { danger: true });
  opButton('get_storyboard', '絵コンテを見る', (spec) => storyboardDialog(dctx, spec));
  opButton('get_quality_review', '品質レビューを見る', (spec) => qualityDialog(dctx, spec));
  const broken = ops.malformedPaid ?? [];
  const warning = broken.length
    ? h('div', { class: 'alert alert-warn', role: 'alert' }, h('p', { class: 'alert-title' }, 'カタログが不正です'), h('p', {}, `operations.json の paid が不正な操作（${broken.join(', ')}）は有料として扱います。サーバーの配信状態を確認してください。`))
    : null;
  return h('div', {}, warning, bar, h('details', {}, h('summary', {}, '各操作に必要な権限'), hints));
}

export function changeSetsPanel(dctx, workspace) {
  const items = workspace.pending_change_sets ?? [];
  const panel = h('section', { 'aria-labelledby': 'cs-h' }, h('h3', { id: 'cs-h' }, '変更案（承認待ち）'));
  if (items.length === 0) {
    panel.append(h('p', {}, '承認待ちの変更案はありません。'));
    return panel;
  }
  for (const cs of items) {
    const row = h('div', { class: 'panel' });
    row.append(
      kv([
        ['変更案', cs.change_set_id],
        ['種類 / 状態', `${cs.kind} / ${cs.status}`],
        ['基準 revision', cs.base_revision_id],
        ['費用の見積り', cs.estimated_cost_usd === null || cs.estimated_cost_usd === undefined ? COST_NOT_ESTIMATED : formatUsd(cs.estimated_cost_usd)],
        ['提案者', `${cs.actor_type}`],
      ]),
    );
    const previewSpec = dctx.ops.byName.get('preview_change_impact');
    const applySpec = dctx.ops.byName.get('apply_approved_change');
    const a = h('div', { class: 'actions' });
    const pb = h('button', { type: 'button', class: 'btn', onclick: () => previewDialog(dctx, previewSpec, cs) }, '影響を見る');
    const ab = h('button', { type: 'button', class: 'btn btn-primary', onclick: () => applyDialog(dctx, applySpec, previewSpec, cs) }, '承認して適用…');
    if (!previewSpec) { pb.disabled = true; pb.title = '操作カタログに無い操作です'; }
    if (!applySpec || !previewSpec) { ab.disabled = true; ab.title = '操作カタログに無い操作です'; }
    a.append(pb, ab);
    row.append(a);
    panel.append(row);
  }
  return panel;
}

// ---------------------------------------------------------------- dialogs

function holdDialog(dctx, spec) {
  const reason = h('textarea', { id: 'hold-reason', maxlength: HOLD_REASON_MAX });
  openOperationDialog(dctx, {
    title: '一時停止を要求（hold）',
    needsRevision: true,
    episodeId: dctx.episodeId,
    submitLabel: 'hold を要求する',
    intro: h('p', {}, '一時停止を「要求」します。要求の受付は、直ちに止まったことを意味しません。制御状態は画面の「制御」欄で確認してください。'),
    prepare: async (c) => c.append(field('理由（任意）', reason, `${HOLD_REASON_MAX} 文字まで`)),
    validate: () => validateHold({ reason: reason.value }),
    send: (commandId, cid, { controlRevision }) =>
      dctx.api.post(buildPath(spec, { episode_id: dctx.episodeId }), buildHoldBody({ commandId, expectedControlRevision: controlRevision, reason: reason.value }), { correlationId: cid }),
    renderSuccess: acceptedView,
  });
}

function resumeDialog(dctx, spec, ws) {
  const legacy = ws.episode.pipeline_kind === 'legacy';
  const ack = h('input', { type: 'checkbox', id: 'resume-ack' });
  const select = h('select', { id: 'resume-stage' }, h('option', { value: '' }, '指定しない（既定）'), LEGACY_START_STAGES.map((s) => h('option', { value: s }, s)));
  openOperationDialog(dctx, {
    title: '再開（resume）',
    needsRevision: true,
    episodeId: dctx.episodeId,
    submitLabel: '再開する',
    intro: h(
      'div',
      {},
      spec.paid ? h('div', { class: 'alert alert-warn' }, h('p', { class: 'alert-title' }, '有料の生成が始まりうります'), h('p', {}, '再開は固定の計画を歩き、未完了の stage で外部サービスの有料生成を行うことがあります（完了済みの stage は再利用）。')) : null,
      h('p', {}, `種別: ${ws.episode.pipeline_kind}。現在の制御状態: ${ws.read_model?.control?.control_state ?? MISSING}（revision ${ws.control_revision}）`),
      legacy
        ? h('p', {}, 'Legacy は開始する工程を指定できます。')
        : h('p', {}, 'Agentic は固定の計画を先頭から歩きます（完了済みの stage は再利用）。開始工程は指定できません。'),
      h('p', { class: 'muted' }, '送信後、worker の確認結果（worker_check）を表示します。確認できない場合は「未確認」と表示します。'),
    ),
    prepare: async (c) => {
      if (legacy) c.append(field('開始工程 (start_stage)', select));
      if (spec.paid) c.append(h('label', { class: 'checkbox-row', for: 'resume-ack' }, ack, '有料の生成が発生しうることを理解しました'));
    },
    validate: () => (spec.paid && !ack.checked ? '有料の生成が発生しうることへの確認にチェックしてください' : null),
    send: (commandId, cid, { controlRevision }) =>
      dctx.api.post(
        buildPath(spec, { episode_id: dctx.episodeId }),
        buildResumeBody({ commandId, expectedControlRevision: controlRevision, startStage: legacy ? select.value : null }),
        { correlationId: cid },
      ),
    renderSuccess: (d) =>
      h('div', {}, acceptedView(d), kv([['種別', d.pipeline_kind], ['worker の確認 (worker_check)', `${labelOf(WORKER_CHECK_LABEL, d.worker_check)}（${d.worker_check}）`]])),
  });
}

function proposeDialog(dctx, spec, ws) {
  const sbSpec = dctx.ops.byName.get('get_storyboard');
  let storyboard = null;
  const scene = h('select', { id: 'prop-scene' });
  const text = h('textarea', { id: 'prop-text', maxlength: VISUAL_DESCRIPTION_MAX });
  const rationale = h('textarea', { id: 'prop-rationale', maxlength: 4000 });
  const current = h('p', { class: 'muted' });
  const original = () => storyboard?.scenes.find((s) => s.scene_id === scene.value)?.visual_description ?? '';
  scene.addEventListener('change', () => { text.value = original(); current.textContent = `現在: ${original()}`; });
  openOperationDialog(dctx, {
    title: '絵コンテの変更を提案',
    needsRevision: true,
    episodeId: dctx.episodeId,
    submitLabel: '変更案を作る',
    intro: h('p', {}, '編集できるのは visual_description だけです（framing などは未対応）。提案は適用ではありません。作成後に「影響を見る」で費用と影響を確認し、別操作で承認・適用します。'),
    prepare: async (c) => {
      if (!sbSpec) throw new Error('get_storyboard がカタログにありません');
      const { data } = await dctx.api.get(buildPath(sbSpec, { episode_id: dctx.episodeId }));
      storyboard = data.storyboard;
      if (!storyboard) { c.append(h('div', { class: 'alert alert-warn' }, '現行の絵コンテがありません。提案できません。')); return; }
      scene.append(...storyboard.scenes.map((s) => h('option', { value: s.scene_id }, `${s.order}. ${s.scene_id}`)));
      text.value = original();
      current.textContent = `現在: ${original()}`;
      c.append(field('シーン', scene), current, field(`新しい visual_description（${VISUAL_DESCRIPTION_MAX} 文字まで）`, text), field('理由（任意）', rationale));
    },
    validate: () => !storyboard ? '現行の絵コンテがありません' : validateProposal({ sceneId: scene.value, original: original(), text: text.value, rationale: rationale.value }),
    send: (commandId, cid, { controlRevision }) =>
      dctx.api.post(
        buildPath(spec, { episode_id: dctx.episodeId }),
        buildProposeBody({ commandId, expectedControlRevision: controlRevision, episodeId: dctx.episodeId, storyboard, sceneId: scene.value, text: text.value, rationale: rationale.value }),
        { correlationId: cid },
      ),
    renderSuccess: (d) => h('div', {}, kv([['リクエスト', d.request_command_id]]), (d.commands ?? []).map((c) => h('div', {}, `${c.scene_id}: ${labelOf(COMMAND_STATUS_LABEL, c.status)}${c.created ? '' : '（同じ内容の再送）'}`)), h('p', {}, '変更案は「変更案（承認待ち）」に表示されます。')),
  });
  void ws;
}

function previewDialog(dctx, spec, cs) {
  openReadDialog(dctx, {
    title: '変更の影響（プレビュー）',
    episodeId: dctx.episodeId,
    load: async () => {
      const { data } = await dctx.api.get(buildPath(spec, { episode_id: dctx.episodeId, change_set_id: cs.change_set_id }));
      return previewNodes(data);
    },
  });
}

function applyDialog(dctx, applySpec, previewSpec, cs) {
  let preview = null;
  const input = h('input', { type: 'number', id: 'apply-max', min: '0', step: '0.01', inputmode: 'decimal' });
  const ack = h('input', { type: 'checkbox', id: 'apply-ack' });
  openOperationDialog(dctx, {
    title: '承認して適用',
    episodeId: dctx.episodeId,
    submitLabel: '承認して適用する',
    danger: true,
    intro: h('p', {}, '先に影響と費用の見積りを読み込みます。見積りが無い・適用できない理由がある変更は送信できません。承認する上限額は、見積りを見て自分で入力してください（既定値はありません）。'),
    prepare: async (c) => {
      const { data } = await dctx.api.get(buildPath(previewSpec, { episode_id: dctx.episodeId, change_set_id: cs.change_set_id }));
      preview = data;
      c.replaceChildren(
        previewNodes(preview),
        field('承認する上限額 (approved_max_cost_usd, USD)', input, preview.estimated_cost_usd ? `見積り $${preview.estimated_cost_usd} 以上` : ''),
        h('label', { class: 'checkbox-row', for: 'apply-ack' }, ack, '見積りの範囲で有料の再生成が発生しうることを理解しました'),
      );
    },
    validate: () => {
      if (preview && preview.control_revision !== dctx.getWorkspace().control_revision) {
        dctx.reloadWorkspace();
        return '画面を開いた後に制御状態が変わりました。最新を読み込んだので、閉じてやり直してください。';
      }
      return validateApply({ preview, input: input.value, acknowledged: ack.checked });
    },
    send: (commandId, cid) =>
      dctx.api.post(
        buildPath(applySpec, { episode_id: dctx.episodeId, change_set_id: cs.change_set_id }),
        buildApplyBody({ commandId, expectedControlRevision: preview.control_revision, input: input.value }),
        { correlationId: cid },
      ),
    renderSuccess: acceptedView,
  });
}

function startDialog(dctx, spec, ws) {
  const ack = h('input', { type: 'checkbox', id: 'start-ack' });
  openOperationDialog(dctx, {
    title: '制御を開始（有料の生成）',
    episodeId: dctx.episodeId,
    submitLabel: '制作を開始する',
    danger: true,
    intro: h(
      'div',
      {},
      h('div', { class: 'alert alert-warn' }, h('p', { class: 'alert-title' }, '有料の生成が始まります'), h('p', {}, 'この操作は外部サービスの有料生成を伴います。人だけが実行できる操作です（AI アシスタントは実行できません）。')),
      h('p', {}, `種別: ${ws.episode.pipeline_kind} / 現在の状態: ${ws.episode.status}`),
      h('p', { class: 'muted' }, '同じ Episode の workflow が既にあれば二重には始めません。'),
    ),
    prepare: async (c) => c.append(h('label', { class: 'checkbox-row', for: 'start-ack' }, ack, '有料の生成が始まることを理解しました')),
    validate: () => (ack.checked ? null : '有料の生成が始まることへの確認にチェックしてください'),
    send: (_commandId, cid) => dctx.api.post(buildPath(spec, { episode_id: dctx.episodeId }), undefined, { correlationId: cid }),
    renderSuccess: (d) =>
      kv([
        ['workflow_id', d.workflow_id],
        ['開始', d.started ? '投入しました（実行中とは限りません）' : '既に開始済み（二重には始めていません）'],
        ['worker の確認 (worker_check)', `${labelOf(WORKER_CHECK_LABEL, d.worker_check)}（${d.worker_check}）`],
      ]),
  });
}

function storyboardDialog(dctx, spec) {
  openReadDialog(dctx, {
    title: '絵コンテ',
    episodeId: dctx.episodeId,
    load: async () => {
      const { data } = await dctx.api.get(buildPath(spec, { episode_id: dctx.episodeId }));
      const sb = data.storyboard;
      if (!sb) return h('p', {}, '現行の絵コンテはありません。');
      return h(
        'div',
        {},
        kv([['revision', sb.revision_id], ['manifest', sb.manifest_id], ['編集できる属性', (sb.editable_fields ?? []).join(', ')], ['未対応の属性', (sb.unsupported_fields ?? []).join(', ')]]),
        sb.scenes.map((s) => h('div', { class: 'panel' }, kv([['シーン', `${s.order}. ${s.scene_id}`], ['visual_description', s.visual_description], ['framing', s.framing], ['camera_movement', s.camera_movement], ['尺(ms)', s.duration_ms]]))),
      );
    },
  });
}

function qualityDialog(dctx, spec) {
  openReadDialog(dctx, {
    title: '品質レビュー',
    episodeId: dctx.episodeId,
    load: async () => {
      const { data } = await dctx.api.get(buildPath(spec, { episode_id: dctx.episodeId }));
      if (data.status === 'not_reviewed') return h('p', {}, badge('未レビュー', 'neutral'), ' 品質レビューはまだ記録されていません。');
      return h('div', {}, kv([['状態', data.status], ['artifact', data.artifact_id]]), h('h4', {}, 'レビュー内容（自己評価）'), tree(data.review), h('h4', {}, '指摘 (findings)'), tree(data.findings));
    },
  });
}
