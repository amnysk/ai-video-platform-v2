import { h, replaceChildren } from '../dom.js';
import { badge, errorBox, kv, listOrMissing, loading, missing } from '../components.js';
import { buildPath } from '../operations.js';
import { extractStages } from '../stages.js';
import { startPoller } from '../poller.js';
import { episodeStatusLabel, needsAttention, pipelineKindLabel } from '../status.js';
import { LLM_COST_UNMEASURED_NOTE, MISSING, formatDateTime, formatUsd } from '../format.js';
import { stageCard } from './stage_card.js';
import { auditSection } from './audit.js';
import { changeSetsPanel, interventionBar } from './interventions.js';

const POLL_MS = 10_000;

export function detailView(ctx, episodeId) {
  const wsSpec = ctx.ops.byName.get('get_episode_workspace');
  const progSpec = ctx.ops.byName.get('get_workflow_progress');
  let workspace = null;
  let progress = null;
  let progressError = null;
  let disposed = false;
  const openStages = new Set();
  let audit = null;

  const root = h('section', {}, h('h2', {}, 'Episode 詳細'), loading());
  const top = h('div', {});
  const stagesBox = h('div', {});
  const auditBox = h('div', {});
  const csBox = h('div', {});
  const notice = h('div', {});

  const dctx = {
    api: ctx.api,
    ops: ctx.ops,
    uuid: ctx.uuid,
    announce: ctx.announce,
    episodeId,
    knownScopes: null, // principal の scope を知る API は無い。権限判定はサーバー
    getWorkspace: () => workspace,
    reloadWorkspace: () => loadWorkspace(),
  };

  async function loadWorkspace() {
    try {
      const { data } = await ctx.api.get(buildPath(wsSpec, { episode_id: episodeId }));
      if (disposed) return;
      if (String(data?.episode?.episode_id).toLowerCase() !== episodeId) {
        replaceChildren(root, h('h2', {}, 'Episode 詳細'), h('div', { class: 'alert alert-error', role: 'alert' }, 'URL の Episode と返ってきたデータの Episode が一致しません。表示を中止しました。'));
        workspace = null;
        return;
      }
      workspace = data;
      replaceChildren(notice);
      await loadProgress();
      renderAll();
    } catch (err) {
      if (disposed || err?.name === 'AbortError') return;
      if (!workspace) replaceChildren(root, h('h2', {}, 'Episode 詳細'), errorBox(err, { onRetry: loadWorkspace }), h('p', {}, h('a', { href: '#/' }, '一覧に戻る')));
      else replaceChildren(notice, errorBox(err, { onRetry: loadWorkspace }));
    }
  }

  async function loadProgress() {
    if (!progSpec) return;
    try {
      const { data } = await ctx.api.get(buildPath(progSpec, { episode_id: episodeId }));
      if (disposed) return;
      progress = data;
      progressError = null;
    } catch (err) {
      if (disposed || err?.name === 'AbortError') return;
      progressError = err;
    }
    if (workspace) renderStages();
  }

  function renderTop() {
    const w = workspace;
    const e = w.episode;
    const rm = w.read_model ?? {};
    const c = rm.control ?? {};
    const attn = needsAttention(e.status);
    const { episodeFailure } = extractStages(w, progress);
    const costs = rm.costs;
    replaceChildren(
      top,
      h('p', {}, h('a', { href: '#/' }, '← 一覧に戻る')),
      h('h2', {}, e.topic ? e.topic : '（トピック未記録）'),
      kv([
        ['Episode ID', h('span', { class: 'episode-id' }, e.episode_id)],
        ['状態', h('span', {}, `${episodeStatusLabel(e.status)}（${e.status}） `, attn ? badge(`要注意: ${attn}`, 'attention') : null)],
        ['種別', pipelineKindLabel(e.pipeline_kind)],
        ['制御', c.control_state ? `${c.control_state}（モード ${c.mode ?? MISSING}、revision ${w.control_revision}）${c.persisted === false ? ' ※制御行が無く既定値で表示' : ''}` : null],
        ['待機地点', c.waiting_point],
        ['停止要求 / 停止済み', `${formatDateTime(c.hold_requested_at)} / ${formatDateTime(c.held_at)}`],
        ['blocked の理由', rm.blocked_reason],
        ['workflow_id', w.workflow_id],
        ['採用 manifest', rm.manifest?.manifest_id],
        ['作成 / 更新', `${formatDateTime(e.created_at)} / ${formatDateTime(e.updated_at)}`],
      ]),
      episodeFailure ? h('div', { class: 'alert alert-error', role: 'alert' }, h('p', { class: 'alert-title' }, '直近の失敗（ステージに帰属できないもの）'), kv([['code', episodeFailure.code], ['message', episodeFailure.message], ['発生源', `${episodeFailure.source} ${episodeFailure.sourceId ?? MISSING}`], ['日時', formatDateTime(episodeFailure.at)]])) : null,
      h(
        'div',
        { class: 'panel' },
        h('h3', {}, '費用'),
        costs
          ? kv([
              ['外部生成の予約（見積り）', formatUsd(costs.provider_usd)],
              ['Research（見積り）', formatUsd(costs.research_usd)],
              ['計測済みの合計', formatUsd(costs.measured_usd)],
              ['見積りの記録が無い呼び出し', (costs.unmeasured ?? []).length === 0 ? 'なし' : listOrMissing(costs.unmeasured, (u) => `${u.provider}: ${u.calls} 回（0 円ではなく未計測）`)],
              ['LLM の費用', costs.llm === 'unmeasured' ? LLM_COST_UNMEASURED_NOTE : costs.llm],
            ])
          : missing(),
      ),
      h('h3', {}, '介入操作'),
      interventionBar(dctx, () => workspace),
    );
    replaceChildren(csBox, changeSetsPanel(dctx, w));
  }

  function renderStages() {
    const { stages, unclassified } = extractStages(workspace, progress);
    const cards = stages.map((s) =>
      stageCard(s, {
        open: openStages.has(s.id),
        onToggle: (id, open) => (open ? openStages.add(id) : openStages.delete(id)),
      }),
    );
    if (unclassified) {
      cards.push(stageCard(unclassified, { open: openStages.has('other'), onToggle: (id, open) => (open ? openStages.add(id) : openStages.delete(id)) }));
    }
    replaceChildren(
      stagesBox,
      h('h3', {}, 'ステージ'),
      h('p', { class: 'muted small' }, `経過 (progress) は前面表示中のみ ${POLL_MS / 1000} 秒ごとに更新します。最終取得: ${progress ? '取得済み' : MISSING}`),
      progressError ? errorBox(progressError) : null,
      cards,
    );
  }

  function renderAll() {
    renderTop();
    renderStages();
    if (!audit) {
      audit = auditSection(() => workspace?.read_model);
      replaceChildren(auditBox, audit.el);
    } else audit.refresh();
    replaceChildren(root, notice, top, csBox, stagesBox, auditBox);
  }

  let poller = null;
  const onVis = () => poller?.pokeIfVisible();

  loadWorkspace().then(() => {
    if (disposed) return;
    poller = startPoller({ fn: loadProgress, intervalMs: POLL_MS, isVisible: () => document.visibilityState === 'visible' });
    document.addEventListener('visibilitychange', onVis);
  });

  return {
    el: root,
    dispose() {
      disposed = true;
      poller?.stop();
      document.removeEventListener('visibilitychange', onVis);
    },
  };
}
