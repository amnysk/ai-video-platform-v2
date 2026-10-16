// 書き込み操作の request body の組み立てと入力検査。DOM 無し。
// キーは operations.json の body_fields と一致しなければならない（tests/web/requests.test.mjs）。
// 検査は利用者への親切であり、権限・規則の最終判定はサーバー。

export const VISUAL_DESCRIPTION_MAX = 600; // contracts/intervention.py SceneOverrideVisual と同じ
export const HOLD_REASON_MAX = 1000;
export const RATIONALE_MAX = 4000;
/** legacy の resume が受ける start_stage（contracts/pipeline.py PipelineStage の値） */
export const LEGACY_START_STAGES = ['script', 'storyboard', 'production', 'render', 'upload'];

export function buildHoldBody({ commandId, expectedControlRevision, reason }) {
  const r = (reason ?? '').trim();
  return { command_id: commandId, expected_control_revision: expectedControlRevision, reason: r || null };
}
export function validateHold({ reason }) {
  if ((reason ?? '').length > HOLD_REASON_MAX) return `理由は ${HOLD_REASON_MAX} 文字以内にしてください`;
  return null;
}

export function buildResumeBody({ commandId, expectedControlRevision, startStage }) {
  return { command_id: commandId, expected_control_revision: expectedControlRevision, start_stage: startStage || null };
}

export function validateProposal({ sceneId, original, text, rationale }) {
  if (!sceneId) return 'シーンを選んでください';
  const t = (text ?? '').trim();
  if (!t) return '新しい visual_description を入力してください';
  if (t.length > VISUAL_DESCRIPTION_MAX) return `visual_description は ${VISUAL_DESCRIPTION_MAX} 文字以内にしてください`;
  if (t === (original ?? '').trim()) return '現在の内容と同じです。変更してください';
  if ((rationale ?? '').length > RATIONALE_MAX) return `理由は ${RATIONALE_MAX} 文字以内にしてください`;
  return null;
}

export function buildProposeBody({ commandId, expectedControlRevision, episodeId, storyboard, sceneId, text, rationale }) {
  const r = (rationale ?? '').trim();
  return {
    command_id: commandId,
    expected_control_revision: expectedControlRevision,
    proposal: {
      episode_id: episodeId,
      base_manifest_id: storyboard.manifest_id ?? null,
      base_storyboard_revision_id: storyboard.revision_id,
      // P2 で編集できるのは visual_description だけ（他の属性は送らない）
      edits: [{ scene_id: sceneId, visual_description: text.trim() }],
      rationale: r || null,
    },
  };
}

/** 適用前の検査。見積りが無い・blocker がある・上限が見積り未満なら送らない。 */
export function validateApply({ preview, input, acknowledged }) {
  if (!preview) return '影響の見積りを読み込めていません';
  if (preview.status !== 'proposed') return `この変更案は適用できる状態ではありません（${preview.status}）`;
  if (preview.estimated_cost_usd === null || preview.estimated_cost_usd === undefined || !preview.cost_estimated) {
    return '費用が見積もられていないため適用できません（未計測を 0 円として扱いません）';
  }
  if (Array.isArray(preview.blockers) && preview.blockers.length > 0) return '適用できない理由（blocker）があります';
  const estimate = Number(preview.estimated_cost_usd);
  if (!Number.isFinite(estimate)) return '見積り額を読み取れません';
  const raw = String(input ?? '').trim();
  if (raw === '') return '承認する上限額（USD）を入力してください';
  const max = Number(raw);
  if (!Number.isFinite(max) || max < 0) return '上限額は 0 以上の数値にしてください';
  if (max < estimate) return `上限額は見積り（$${preview.estimated_cost_usd}）以上にしてください`;
  if (estimate > 0 && !acknowledged) return '有料の再生成が発生しうることへの確認にチェックしてください';
  return null;
}

export function buildApplyBody({ commandId, expectedControlRevision, input }) {
  return { command_id: commandId, expected_control_revision: expectedControlRevision, approved_max_cost_usd: Number(String(input).trim()) };
}

/** 送信直前の再確認: 画面の route の Episode と、読み込んだ workspace の Episode が同じか。 */
export function verifyEpisode(routeId, workspace) {
  const wid = workspace?.episode?.episode_id;
  if (!routeId || !wid || String(routeId).toLowerCase() !== String(wid).toLowerCase()) {
    return 'URL の Episode と読み込み済みのデータの Episode が一致しません。送信を中止しました';
  }
  return null;
}
