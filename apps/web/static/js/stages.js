// workspace.read_model / progress から Episode 詳細の「ステージ別」ビューを組む。DOM 無し。
//
// 原則: 記録が無い項目は null/空で返し、描画側が「未記録」と出す。推測・補完しない。
// ステージへの振り分けは下の定数表だけが決める（読み取りモデルに stage 欄は無い）。表に無いものは
// 「ステージ未分類」に置く（捨てない・無理に当てはめない）。

export const STAGES = [
  { id: 'research', label: 'Research' },
  { id: 'strategy', label: 'Strategy・Brief' },
  { id: 'script', label: 'Script' },
  { id: 'storyboard', label: 'Storyboard' },
  { id: 'production', label: 'Production' },
  { id: 'render', label: 'Render' },
  { id: 'upload', label: 'Upload' },
];
export const OTHER_STAGE = { id: 'other', label: 'ステージ未分類' };

/** manifest member の artifact_type（contracts/states.py ArtifactType） -> stage */
export const ARTIFACT_TYPE_STAGE = {
  user_brief: 'strategy',
  strategy_brief: 'strategy',
  creative_brief: 'strategy',
  script: 'script',
  storyboard: 'storyboard',
  scene_image: 'production',
  scene_voice: 'production',
  scene_video: 'production',
  production_manifest: 'production',
  scene_visual_override: 'production',
  final_video: 'render',
  upload_receipt: 'upload',
};
/** decision_records.decision_type -> stage */
export const DECISION_TYPE_STAGE = {
  choose_topic: 'strategy',
  write_script: 'script',
  plan_storyboard: 'storyboard',
  apply_change: 'storyboard',
};
/** agent_runs.step -> stage（step が優先） */
export const AGENT_STEP_STAGE = {
  choose_topic: 'strategy',
  write_script: 'script',
  plan_storyboard: 'storyboard',
};
/** agent_runs.role -> stage（step で決まらないときだけ） */
export const AGENT_ROLE_STAGE = { strategy: 'strategy', creative_director: 'strategy' };
/** progress.jobs[].type（contracts/states.py JobType） -> stage */
export const JOB_TYPE_STAGE = {
  write_script: 'script',
  plan_storyboard: 'storyboard',
  produce_scene_image: 'production',
  produce_scene_voice: 'production',
  produce_scene_video: 'production',
  assemble_production: 'production',
  plan_scene_alternative: 'production',
  render_final_video: 'render',
  upload_final_video: 'upload',
};
/** command.type -> stage（storyboard の変更案だけが対象を持つ。他は stage 無し） */
export const COMMAND_TYPE_STAGE = { propose_change: 'storyboard', apply_change: 'storyboard' };
/** change_sets.kind（contracts/intervention.py ChangeKind） -> stage */
export const CHANGE_KIND_STAGE = { 'scene.override_visual': 'storyboard', 'script.edit_scenes': 'script' };
/** episode_events.refs に stage がある場合のキー */
export const EVENT_STAGE_REF_KEYS = ['stage', 'stage_id'];

const get = (table, key, fallback = 'other') => (typeof key === 'string' && Object.hasOwn(table, key) ? table[key] : fallback);

export const stageOfArtifactType = (t) => get(ARTIFACT_TYPE_STAGE, t);
export const stageOfDecisionType = (t) => get(DECISION_TYPE_STAGE, t);
export const stageOfJobType = (t) => get(JOB_TYPE_STAGE, t);
export function stageOfAgentRun(run) {
  const byStep = get(AGENT_STEP_STAGE, run?.step, null);
  return byStep ?? get(AGENT_ROLE_STAGE, run?.role);
}

// ---------------------------------------------------------------- 正規化

export function normalizeDecision(d) {
  const p = d?.payload && typeof d.payload === 'object' ? d.payload : {};
  const candidates = Array.isArray(p.candidates) ? p.candidates : [];
  return {
    id: d.decision_id ?? null,
    type: d.decision_type ?? null,
    stage: stageOfDecisionType(d.decision_type),
    actorType: d.actor_type ?? null,
    actorId: d.actor_id ?? null,
    agentRunId: d.agent_run_id ?? null,
    commandId: d.command_id ?? null,
    createdAt: d.created_at ?? null,
    derived: Boolean(d.derived),
    question: p.question ?? null,
    candidates: candidates.map((c) => ({
      id: c.id ?? null,
      summary: c.summary ?? null,
      benefits: Array.isArray(c.benefits) ? c.benefits : [],
      concerns: Array.isArray(c.concerns) ? c.concerns : [],
      scores: c.scores ?? null,
      selfAssessedScores: c.self_assessed_scores ?? null,
      assessmentKind: c.assessment_kind ?? null,
      rejectedReason: c.rejected_reason ?? null,
    })),
    selected: p.selected_candidate ?? null,
    rationaleKind: p.rationale_kind ?? null,
    rationale: p.rationale ?? null,
    evidenceRefs: Array.isArray(p.evidence_refs) ? p.evidence_refs : [],
    evidenceCheck: p.evidence_check ?? null,
    uncertainties: Array.isArray(p.uncertainties) ? p.uncertainties : [],
    inputSnapshotRef: p.input_snapshot_ref ?? null,
    constraints: Array.isArray(p.constraints) ? p.constraints : [],
  };
}

export function toolName(inv) {
  if (inv && typeof inv === 'object') {
    for (const k of ['name', 'tool', 'tool_name']) if (typeof inv[k] === 'string' && inv[k]) return inv[k];
  }
  return null;
}

export function normalizeAgentRun(r) {
  return {
    id: r.agent_run_id,
    role: r.role ?? null,
    step: r.step ?? null,
    stage: stageOfAgentRun(r),
    model: r.model ?? null,
    promptVersion: r.prompt_version ?? null,
    outcome: r.outcome ?? null,
    createdAt: r.created_at ?? null,
    snapshotKey: r.snapshot_object_key ?? null,
    outputRevisionId: r.output_revision_id ?? null,
    toolInvocations: Array.isArray(r.tool_invocations) ? r.tool_invocations.map((t) => ({ name: toolName(t) })) : [],
    tokenUsage: r.token_usage && typeof r.token_usage === 'object' ? r.token_usage : null,
  };
}

function normalizeMember(m) {
  return {
    slot: m.slot,
    revisionId: m.revision_id,
    artifactType: m.artifact_type ?? null,
    sceneId: m.scene_id ?? null,
    version: m.version ?? null,
    createdAt: m.created_at ?? null,
    sha256: m.sha256 ?? null,
    stale: Boolean(m.stale),
    staleReasons: Array.isArray(m.stale_reasons) ? m.stale_reasons : [],
    readable: m.readable !== false,
    upstream: (m.upstream ?? []).map((u) => ({ revisionId: u.revision_id, slot: u.slot ?? null, source: u.source ?? null })),
  };
}

function normalizeJob(j) {
  return {
    id: j.job_id,
    type: j.type,
    stage: stageOfJobType(j.type),
    status: j.status,
    attempts: j.attempts,
    maxAttempts: j.max_attempts,
    sceneId: j.scene_id ?? null,
    updatedAt: j.updated_at ?? null,
  };
}

/** job 群から stage の状態を決める。job が無ければ null（呼び出し側が成果物の有無だけを示す）。 */
export function jobsStatus(jobs) {
  if (jobs.length === 0) return null;
  const has = (s) => jobs.some((j) => j.status === s);
  if (has('terminal_failed')) return 'terminal_failed';
  if (has('retryable_failed')) return 'retryable_failed';
  if (has('running')) return 'running';
  if (has('queued')) return 'queued';
  if (jobs.every((j) => j.status === 'succeeded' || j.status === 'skipped')) return 'succeeded';
  return null;
}

/** job 1 件の復旧可否（表にある事実だけ。無いものは書かない）。 */
export function jobRecoverability(job) {
  switch (job.status) {
    case 'retryable_failed':
      return `自動再試行の対象です（試行 ${job.attempts}/${job.maxAttempts}）`;
    case 'terminal_failed':
      return '再試行されません（terminal_failed）。画面からの復旧は未実装です（ADR-0045）';
    case 'queued':
      return '実行待ちです';
    case 'running':
      return '実行中です';
    default:
      return null;
  }
}

// ---------------------------------------------------------------- 抽出

function emptyStage(def) {
  return {
    id: def.id,
    label: def.label,
    members: [],
    inputs: [],
    evidence: [],
    decisions: [],
    agentRuns: [],
    jobs: [],
    reservations: [],
    failures: [],
    recoverability: [],
    jobStatus: null,
    script: null,
  };
}

/**
 * @param workspace EpisodeWorkspace（read_model を含む）
 * @param progress  WorkflowProgress | null
 * @returns {{stages, unclassified, episodeFailure}}
 */
export function extractStages(workspace, progress = null) {
  const rm = workspace?.read_model ?? {};
  const byId = new Map([...STAGES, OTHER_STAGE].map((d) => [d.id, emptyStage(d)]));
  const at = (id) => byId.get(byId.has(id) ? id : 'other');

  for (const m of rm.manifest?.members ?? []) {
    const nm = normalizeMember(m);
    const st = at(stageOfArtifactType(nm.artifactType));
    st.members.push(nm);
    for (const u of nm.upstream) st.inputs.push({ from: u.slot ?? u.revisionId, revisionId: u.revisionId, toSlot: nm.slot });
  }
  for (const e of rm.evidence ?? []) {
    at('research').evidence.push({
      id: e.research_artifact_id,
      artifactType: e.artifact_type ?? null,
      verdict: e.verdict ?? null,
      isCurrent: Boolean(e.is_current),
      scriptRevisionId: e.script_revision_id ?? null,
    });
  }
  if (rm.script) {
    at('script').script = {
      revisionId: rm.script.revision_id,
      title: rm.script.title ?? null,
      hook: rm.script.hook ?? null,
      language: rm.script.language ?? null,
      totalDurationMs: rm.script.total_duration_ms ?? null,
      segments: (rm.script.segments ?? []).map((s) => ({
        id: s.segment_id,
        narration: s.narration ?? null,
        visual: s.visual ?? null,
        durationMs: s.duration_ms ?? null,
      })),
    };
  }
  for (const d of rm.decisions ?? []) {
    const nd = normalizeDecision(d);
    at(nd.stage).decisions.push(nd);
  }
  for (const r of rm.agent_runs ?? []) {
    const nr = normalizeAgentRun(r);
    at(nr.stage).agentRuns.push(nr);
  }
  const jobs = (progress?.jobs ?? []).map(normalizeJob);
  for (const j of jobs) at(j.stage).jobs.push(j);

  const res = rm.reservations ?? {};
  for (const [kind, list] of [
    ['in_flight', res.in_flight],
    ['unknown_outcome', res.unknown_outcome],
    ['undispatched', res.undispatched],
  ]) {
    for (const r of list ?? []) {
      at('production').reservations.push({
        id: r.reservation_id,
        kind,
        provider: r.provider ?? null,
        status: r.status ?? null,
        sceneId: r.scene_id ?? null,
        estimatedCostUsd: r.estimated_cost_usd ?? null,
      });
    }
  }

  // 失敗の帰属: job / agent_run の id が分かるものだけ stage に付ける。それ以外は Episode 全体の失敗
  let episodeFailure = null;
  const f = progress?.failure ?? null;
  if (f) {
    const failure = { code: f.code, message: f.message ?? null, source: f.source, sourceId: f.source_id ?? null, at: f.at ?? null };
    let target = null;
    if (f.source === 'job') target = jobs.find((j) => j.id === f.source_id)?.stage ?? null;
    if (f.source === 'agent_run') {
      const run = (rm.agent_runs ?? []).find((r) => r.agent_run_id === f.source_id);
      target = run ? stageOfAgentRun(run) : null;
    }
    if (target) at(target).failures.push(failure);
    else episodeFailure = failure;
  }

  for (const st of byId.values()) {
    st.jobStatus = jobsStatus(st.jobs);
    for (const j of st.jobs) {
      const r = jobRecoverability(j);
      if (r) st.recoverability.push(`${j.type}${j.sceneId ? `（${j.sceneId}）` : ''}: ${r}`);
    }
    if (st.reservations.some((r) => r.kind === 'unknown_outcome')) {
      st.recoverability.push('送信結果が不明の予約があります。自動では再送しません（二重課金防止）');
    }
    if (st.members.some((m) => m.stale)) st.recoverability.push('古い（stale）成果物があります');
  }

  const stages = STAGES.map((d) => byId.get(d.id));
  const other = byId.get('other');
  const hasOther =
    other.members.length || other.decisions.length || other.agentRuns.length || other.jobs.length || other.failures.length;
  return { stages, unclassified: hasOther ? other : null, episodeFailure };
}
