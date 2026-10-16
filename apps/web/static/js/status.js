// Episode / Job / Command などの状態の表示名。キーは contracts/states.py・contracts/intervention.py の
// enum の値そのもの（tests/web/status.test.mjs が Python 側と突き合わせる）。

export const EPISODE_STATUS_LABEL = {
  planned: '計画済み',
  in_progress: '進行中',
  needs_work: '要対応（再試行/品質未達）',
  blocked: '人の判断待ち',
  ready_for_review: '確認待ち',
  approved: '承認済み',
  uploaded: '投稿済み',
  analyzed: '分析済み',
  completed: '完了',
  script_ready: '台本完了',
  storyboard_ready: '絵コンテ完了',
  assets_ready: '素材完了',
  render_ready: '動画完成',
  failed: '失敗',
  cancelled: '取り消し',
};

/**
 * 「要注意」バッジの対象。**contracts/states.py の EpisodeStatus の値だけ**を使う
 * （ここに無い状態を推測で要注意にしない）。
 *   failed            失敗（終端）
 *   blocked           予算・権利・分類不能な失敗で人の判断待ち（docs/domain/episode.md）
 *   needs_work        retryable 失敗または品質ゲート未達
 *   ready_for_review  成果物が揃い人の確認待ち
 */
export const NEEDS_ATTENTION = Object.freeze({
  failed: '失敗',
  blocked: '判断待ち',
  needs_work: '要対応',
  ready_for_review: '確認待ち',
});

export function needsAttention(status) {
  return Object.hasOwn(NEEDS_ATTENTION, status) ? NEEDS_ATTENTION[status] : null;
}

export function episodeStatusLabel(status) {
  return Object.hasOwn(EPISODE_STATUS_LABEL, status) ? EPISODE_STATUS_LABEL[status] : '未知の状態';
}

export const PIPELINE_KIND_LABEL = { agentic: 'Agentic', legacy: 'Legacy' };
export function pipelineKindLabel(kind) {
  return Object.hasOwn(PIPELINE_KIND_LABEL, kind) ? PIPELINE_KIND_LABEL[kind] : '未記録';
}

export const JOB_STATUS_LABEL = {
  queued: '待機中',
  running: '実行中',
  succeeded: '成功',
  retryable_failed: '失敗（自動再試行の対象）',
  terminal_failed: '失敗（再試行なし）',
  skipped: 'スキップ',
};
export const COMMAND_STATUS_LABEL = {
  received: '受付済み（未完了）',
  validated: '検証済み（未完了）',
  applying: '適用中（未完了）',
  applied: '適用済み',
  conflict: '競合',
  rejected: '拒否',
  failed: '失敗',
};
export const ACTOR_TYPE_LABEL = {
  human: '人',
  ai_assistant: 'AI アシスタント',
  agent: 'Agent',
  system: 'システム',
};
export const WORKER_CHECK_LABEL = {
  pollers_present:
    'worker の poller を確認できました（ただし実行されることまでは保証しません）',
  no_pollers: 'worker の poller が見つかりません。実行されない可能性があります',
  unknown: 'worker を確認できませんでした（未確認）',
};
export function labelOf(table, value) {
  return Object.hasOwn(table, value) ? table[value] : '未知';
}
