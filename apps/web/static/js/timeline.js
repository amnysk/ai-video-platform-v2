// 監査表示: episode_events / intervention_commands / decision_records / change_sets を
// 1本の時系列にまとめる。DOM 無し。command の payload 全文は出さない（型・対象・理由・revision だけ）。
import { CHANGE_KIND_STAGE, COMMAND_TYPE_STAGE, EVENT_STAGE_REF_KEYS, stageOfDecisionType } from './stages.js';
import { MISSING } from './format.js';

/** actor_type -> フィルタ区分 */
export const ACTOR_GROUP = { human: 'human', ai_assistant: 'ai', agent: 'agent', system: 'system' };
export const ACTOR_GROUPS = [
  { id: 'human', label: '人' },
  { id: 'ai', label: 'AI アシスタント' },
  { id: 'agent', label: 'Agent' },
  { id: 'system', label: 'システム' },
];

function lookup(obj, key) {
  return obj && typeof obj === 'object' && typeof obj[key] === 'string' && obj[key] ? obj[key] : null;
}

/** trace id は、そのフィールドが実際にある場合だけ。無ければ「未記録」。 */
export function traceIdOf(...sources) {
  for (const s of sources) {
    const v = lookup(s, 'trace_id') ?? lookup(s, 'traceId');
    if (v) return v;
  }
  return MISSING;
}
export function correlationIdOf(...sources) {
  for (const s of sources) {
    const v = lookup(s, 'correlation_id');
    if (v) return v;
  }
  return MISSING;
}

function groupOf(actorType) {
  return Object.hasOwn(ACTOR_GROUP, actorType) ? ACTOR_GROUP[actorType] : null;
}

function eventStage(refs) {
  for (const k of EVENT_STAGE_REF_KEYS) {
    const v = lookup(refs, k);
    if (v) return v;
  }
  return null;
}

/** 時刻の昇順（同時刻は kind と元の順で安定）。 */
export function buildAuditEntries(rm) {
  const out = [];
  let order = 0;
  const push = (e) => out.push({ ...e, order: order++ });

  for (const ev of rm?.events ?? []) {
    push({
      kind: 'event',
      at: ev.created_at ?? null,
      stage: eventStage(ev.refs),
      actorType: ev.actor_type ?? null,
      actorGroup: groupOf(ev.actor_type),
      actorId: ev.actor_id ?? null,
      title: ev.event_name,
      fields: [
        ['seq', String(ev.seq)],
        ['command_id', ev.command_id ?? MISSING],
        ['correlation_id', correlationIdOf(ev.refs)],
        ['trace_id', traceIdOf(ev.refs)],
      ],
    });
  }
  for (const c of rm?.commands ?? []) {
    push({
      kind: 'command',
      at: c.received_at ?? null,
      stage: Object.hasOwn(COMMAND_TYPE_STAGE, c.type) ? COMMAND_TYPE_STAGE[c.type] : null,
      actorType: c.actor_type ?? null,
      actorGroup: groupOf(c.actor_type),
      actorId: c.actor_id ?? null,
      title: `command: ${c.type}`,
      status: c.status,
      fields: [
        ['command_id', c.command_id],
        ['status', `${c.status}${c.done ? '' : '（完了ではありません）'}`],
        ['reason', lookup(c.result, 'reason') ?? MISSING],
        ['result', summarizeResult(c.result)],
        ['reject_reason', c.reject_reason ?? MISSING],
        ['correlation_id', correlationIdOf(c.result)],
        ['trace_id', traceIdOf(c.result)],
        ['received_at', c.received_at ?? MISSING],
        ['finished_at', c.finished_at ?? MISSING],
      ],
    });
  }
  for (const d of rm?.decisions ?? []) {
    const p = d.payload ?? {};
    push({
      kind: 'decision',
      at: d.created_at ?? null,
      stage: stageOfDecisionType(d.decision_type),
      actorType: d.actor_type ?? null,
      actorGroup: groupOf(d.actor_type),
      actorId: d.actor_id ?? null,
      title: `判断: ${d.decision_type}`,
      fields: [
        ['decision_id', d.decision_id ?? MISSING],
        ['question', p.question ?? MISSING],
        ['selected', p.selected_candidate ?? MISSING],
        ['rationale_kind', p.rationale_kind ?? MISSING],
        ['command_id', d.command_id ?? MISSING],
        ['trace_id', traceIdOf(d, p)],
        ['derived', d.derived ? '読み取り時に導出（保存された記録ではありません）' : 'false'],
      ],
    });
  }
  for (const cs of rm?.change_sets ?? []) {
    push({
      kind: 'change_set',
      at: cs.created_at ?? null,
      stage: Object.hasOwn(CHANGE_KIND_STAGE, cs.kind) ? CHANGE_KIND_STAGE[cs.kind] : null,
      actorType: cs.actor_type ?? null,
      actorGroup: groupOf(cs.actor_type),
      actorId: cs.actor_id ?? null,
      title: `変更案: ${cs.kind}（${cs.status}）`,
      fields: [
        ['change_set_id', cs.change_set_id],
        ['base_revision', cs.base_revision_id],
        ['applied_revision', cs.applied_revision_id ?? MISSING],
        ['base_manifest', cs.base_manifest_id ?? MISSING],
        ['applied_manifest', cs.applied_manifest_id ?? MISSING],
        ['rationale', cs.rationale ?? MISSING],
        ['proposed_by_command', cs.proposed_by_command_id],
        ['decided_by_command', cs.decided_by_command_id ?? MISSING],
        ['trace_id', traceIdOf(cs)],
      ],
    });
  }
  out.sort((a, b) => {
    const ta = a.at ? Date.parse(a.at) : Number.NaN;
    const tb = b.at ? Date.parse(b.at) : Number.NaN;
    if (Number.isNaN(ta) && Number.isNaN(tb)) return a.order - b.order;
    if (Number.isNaN(ta)) return 1; // 時刻が無いものは末尾（捏造しない）
    if (Number.isNaN(tb)) return -1;
    return ta - tb || a.order - b.order;
  });
  return out;
}

/** result から表示してよいキーだけを短く要約する（全文は出さない）。 */
function summarizeResult(result) {
  if (!result || typeof result !== 'object') return MISSING;
  const keys = ['outcome', 'status', 'message', 'applied_revision_id', 'new_revision_id', 'manifest_id'];
  const parts = [];
  for (const k of keys) if (typeof result[k] === 'string') parts.push(`${k}=${result[k]}`);
  return parts.length ? parts.join(', ') : `（${Object.keys(result).length} 項目あり。全文は表示しません）`;
}

/** actor 区分（'' = すべて）と stage（'' = すべて / '__none__' = stage 無し）で絞る。 */
export function filterEntries(entries, { actor = '', stage = '' } = {}) {
  return entries.filter((e) => {
    if (actor && e.actorGroup !== actor) return false;
    if (stage === '__none__') return e.stage === null;
    if (stage && e.stage !== stage) return false;
    return true;
  });
}
