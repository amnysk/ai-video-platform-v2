// ステージ 1 つの表示。記録の無い項目はすべて「未記録」。推測・補完しない。
import { h } from '../dom.js';
import { badge, kv, listOrMissing, missing, valueOrMissing } from '../components.js';
import { JOB_STATUS_LABEL, labelOf } from '../status.js';
import { formatDateTime, formatUsd, MISSING, shortId } from '../format.js';

const RATIONALE_KIND_LABEL = {
  reported: 'モデルの自己申告',
  post_hoc_summary: '採用後に生成した説明',
  human: '人の判断',
  rule: '決定論の規則',
  none: '理由の記録なし',
};
const ASSESSMENT_LABEL = { self_assessed: '自己評価', measured: '実測', rule: '規則' };
const EVIDENCE_CHECK_LABEL = {
  verified_in_input: '入力に存在を確認済み',
  unverified_ref: '入力に無い資料を根拠に挙げている（未確認）',
  not_checked: '未検証',
};

function stageStatus(stage) {
  if (stage.jobStatus) {
    const kind = stage.jobStatus === 'succeeded' ? 'ok' : stage.jobStatus.endsWith('failed') ? 'attention' : 'neutral';
    return badge(labelOf(JOB_STATUS_LABEL, stage.jobStatus), kind);
  }
  if (stage.members.length > 0) return badge('成果物あり（job の記録なし）', 'neutral');
  return badge(MISSING, 'neutral');
}

function scoresView(scores, kind) {
  if (!scores) return null;
  const k = kind ? labelOf(ASSESSMENT_LABEL, kind) : MISSING;
  return h('div', { class: 'small' }, `スコア（種別: ${k}）: `, Object.entries(scores).map(([n, v]) => `${n}=${v}`).join(', '));
}

function decisionView(d) {
  const box = h('div', { class: 'panel' });
  box.append(
    h('h4', {}, `判断: ${d.type ?? MISSING}`, d.derived ? ' ' : null, d.derived ? badge('読み取り時に導出', 'warn') : null),
    kv([
      ['問い', d.question],
      ['判断した主体', d.actorType ? `${d.actorType} / ${d.actorId ?? MISSING}` : null],
      ['日時', formatDateTime(d.createdAt)],
    ]),
  );
  const cands = h('div', {});
  if (d.candidates.length === 0) cands.append(missing());
  for (const c of d.candidates) {
    const sel = d.selected !== null && c.id === d.selected;
    const el = h('div', { class: `candidate${sel ? ' selected' : ''}` });
    el.append(
      h('div', {}, h('strong', {}, `候補 ${c.id ?? MISSING}`), ' ', sel ? badge('採用', 'ok') : null),
      h('div', {}, valueOrMissing(c.summary)),
    );
    if (c.benefits.length) el.append(h('div', { class: 'small' }, '利点: ', c.benefits.join(' / ')));
    if (c.concerns.length) el.append(h('div', { class: 'small' }, '懸念: ', c.concerns.join(' / ')));
    const s1 = scoresView(c.scores, c.assessmentKind);
    const s2 = scoresView(c.selfAssessedScores, 'self_assessed');
    if (s1) el.append(s1);
    if (s2) el.append(s2);
    if (!sel) el.append(h('div', { class: 'small' }, '却下理由: ', valueOrMissing(c.rejectedReason)));
    cands.append(el);
  }
  box.append(h('h4', {}, '候補'), cands);
  if (d.selected === null) box.append(kv([['採用した候補', null]]));
  const rk = d.rationaleKind ? labelOf(RATIONALE_KIND_LABEL, d.rationaleKind) : null;
  box.append(
    kv([
      ['理由の種別 (rationale_kind)', d.rationaleKind ? `${rk}（${d.rationaleKind}）` : null],
      ['理由 (rationale)', d.rationale],
      ['根拠の検証', d.evidenceCheck ? labelOf(EVIDENCE_CHECK_LABEL, d.evidenceCheck) : null],
      ['根拠 (evidence_refs)', listOrMissing(d.evidenceRefs)],
      ['不確実性 (uncertainties)', listOrMissing(d.uncertainties)],
      ['入力 snapshot', d.inputSnapshotRef],
    ]),
  );
  return box;
}

function agentRunView(r) {
  const usage = r.tokenUsage
    ? Object.entries(r.tokenUsage).map(([k, v]) => `${k}=${typeof v === 'object' ? '{…}' : v}`).join(', ')
    : null;
  return h(
    'div',
    { class: 'panel' },
    h('h4', {}, `Agent 実行: ${r.role ?? MISSING} / ${r.step ?? MISSING}`),
    kv([
      ['実行 ID', shortId(r.id)],
      ['model', r.model],
      ['prompt_version', r.promptVersion],
      ['結果', r.outcome],
      ['ツール呼び出し', r.toolInvocations.length ? `${r.toolInvocations.length} 件` : null],
      ['ツール名', r.toolInvocations.length ? r.toolInvocations.map((t) => t.name ?? MISSING).join(', ') : null],
      ['token_usage', usage],
      ['入力 snapshot', r.snapshotKey],
      ['日時', formatDateTime(r.createdAt)],
    ]),
  );
}

export function stageCard(stage, { open, onToggle, extra }) {
  const details = h('details', { class: 'stage', id: `stage-${stage.id}` });
  if (open) details.setAttribute('open', '');
  details.addEventListener('toggle', () => onToggle(stage.id, details.open));
  const failedHere = stage.failures.length > 0 || stage.jobStatus === 'terminal_failed' || stage.jobStatus === 'retryable_failed';
  details.append(h('summary', {}, h('span', {}, stage.label), stageStatus(stage), failedHere ? badge('失敗あり', 'attention') : null));

  const body = h('div', { class: 'stage-body' });

  // 入力
  body.append(h('h4', {}, '入力'));
  const inputs = [];
  for (const i of stage.inputs) inputs.push(`${i.from}（→ ${i.toSlot}）`);
  for (const r of stage.agentRuns) if (r.snapshotKey) inputs.push(`Agent 入力 snapshot: ${r.snapshotKey}`);
  for (const d of stage.decisions) if (d.inputSnapshotRef) inputs.push(`判断の入力 snapshot: ${d.inputSnapshotRef}`);
  body.append(listOrMissing([...new Set(inputs)]));

  // 出力
  body.append(h('h4', {}, '出力（成果物）'));
  body.append(
    listOrMissing(stage.members, (m) =>
      h('span', {}, `${m.slot} / ${m.artifactType ?? MISSING} / v${m.version ?? MISSING} / ${shortId(m.revisionId)} `, m.stale ? badge(`stale: ${m.staleReasons.join(', ') || MISSING}`, 'warn') : null),
    ),
  );
  if (stage.id === 'research') {
    body.append(h('h4', {}, 'Research の根拠 (evidence)'));
    body.append(
      listOrMissing(stage.evidence, (e) => `${e.artifactType ?? MISSING} / verdict: ${e.verdict ?? MISSING}${e.isCurrent ? '' : '（現行ではない）'}`),
    );
    body.append(h('p', { class: 'muted small' }, 'Research 専用の read 操作は無く、読み取りモデルに出る範囲だけを表示します（ADR-0045）。'));
  }
  if (stage.script) {
    const s = stage.script;
    body.append(
      h('h4', {}, '台本'),
      kv([['題名', s.title], ['フック', s.hook], ['言語', s.language], ['総尺(ms)', s.totalDurationMs]]),
      h('ol', {}, s.segments.map((g) => h('li', {}, h('div', {}, `ナレーション: `, valueOrMissing(g.narration)), h('div', { class: 'muted' }, '映像: ', valueOrMissing(g.visual))))),
    );
  }
  if (extra) body.append(extra);

  // AI の判断・根拠
  body.append(h('h4', {}, 'AI の判断と根拠'));
  if (stage.decisions.length === 0) body.append(missing('（このステージの判断記録はありません）'));
  for (const d of stage.decisions) body.append(decisionView(d));

  // model / tool
  body.append(h('h4', {}, 'モデル・ツール'));
  if (stage.agentRuns.length === 0) body.append(missing('（Agent 実行の記録はありません）'));
  for (const r of stage.agentRuns) body.append(agentRunView(r));

  // ジョブ・予約
  body.append(h('h4', {}, '実行状況 (job)'));
  body.append(
    listOrMissing(stage.jobs, (j) => `${j.type}${j.sceneId ? `（${j.sceneId}）` : ''}: ${labelOf(JOB_STATUS_LABEL, j.status)} / 試行 ${j.attempts}/${j.maxAttempts} / ${formatDateTime(j.updatedAt)}`),
  );
  if (stage.reservations.length) {
    body.append(h('h4', {}, '外部生成の予約'));
    body.append(
      listOrMissing(stage.reservations, (r) => `${r.provider ?? MISSING}${r.sceneId ? `（${r.sceneId}）` : ''}: ${r.kind} / 見積り ${formatUsd(r.estimatedCostUsd)}`),
    );
  }

  // エラー・復旧
  body.append(h('h4', {}, 'エラーと復旧'));
  if (stage.failures.length === 0) body.append(h('p', {}, 'このステージに帰属する失敗は記録されていません。'));
  for (const f of stage.failures) {
    body.append(
      h('div', { class: 'alert alert-error' }, kv([['code', f.code], ['message', f.message], ['発生源', `${f.source} ${f.sourceId ?? MISSING}`], ['日時', formatDateTime(f.at)]])),
    );
  }
  body.append(h('div', {}, h('strong', {}, '復旧の可否: '), listOrMissing(stage.recoverability)));

  details.append(body);
  return details;
}
