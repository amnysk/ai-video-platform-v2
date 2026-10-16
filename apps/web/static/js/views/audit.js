import { h, replaceChildren } from '../dom.js';
import { badge, kv } from '../components.js';
import { ACTOR_GROUPS, buildAuditEntries, filterEntries } from '../timeline.js';
import { STAGES } from '../stages.js';
import { formatDateTime, MISSING } from '../format.js';

const KIND_LABEL = { event: 'イベント', command: 'command', decision: '判断', change_set: '変更案' };

/** 監査・履歴。正本は DB 由来の read_model（OpenSearch は読まない / ADR-0045 §7）。 */
export function auditSection(getReadModel) {
  const filter = { actor: '', stage: '' };
  let newestFirst = false;
  const list = h('div', { 'aria-live': 'off' });
  const count = h('p', { class: 'muted', role: 'status' });

  const actor = h('select', { id: 'f-actor', onchange: () => { filter.actor = actor.value; draw(); } },
    h('option', { value: '' }, 'すべて'), ACTOR_GROUPS.map((g) => h('option', { value: g.id }, g.label)));
  const stage = h('select', { id: 'f-stage', onchange: () => { filter.stage = stage.value; draw(); } },
    h('option', { value: '' }, 'すべて'), STAGES.map((s) => h('option', { value: s.id }, s.label)), h('option', { value: '__none__' }, 'ステージ無し'));
  const order = h('input', { type: 'checkbox', id: 'f-order', onchange: () => { newestFirst = order.checked; draw(); } });

  function draw() {
    let entries = filterEntries(buildAuditEntries(getReadModel()), filter);
    if (newestFirst) entries = entries.slice().reverse();
    count.textContent = `${entries.length} 件`;
    replaceChildren(
      list,
      entries.length === 0 ? h('p', {}, '該当する記録はありません。') : null,
      entries.map((e) =>
        h(
          'article',
          { class: 'audit-entry' },
          h('h4', {}, badge(KIND_LABEL[e.kind] ?? e.kind), ' ', e.title),
          h('p', { class: 'muted small' }, `${formatDateTime(e.at)} / 主体: ${e.actorType ?? MISSING} ${e.actorId ?? MISSING} / ステージ: ${e.stage ?? MISSING}`),
          kv(e.fields),
        ),
      ),
    );
  }

  const el = h(
    'section',
    { id: 'audit', 'aria-labelledby': 'audit-h' },
    h('h3', { id: 'audit-h' }, '監査・履歴'),
    h('p', { class: 'muted small' }, 'episode_events / intervention_commands / decision_records / change_sets を時系列で併合しています。command の内容の全文は表示しません。trace id は記録がある場合だけ表示します。'),
    h('div', { class: 'toolbar' },
      h('div', {}, h('label', { for: 'f-actor' }, '主体で絞り込み'), actor),
      h('div', {}, h('label', { for: 'f-stage' }, 'ステージで絞り込み'), stage),
      h('div', {}, h('label', { class: 'checkbox-row', for: 'f-order' }, order, '新しい順に並べる'))),
    count,
    list,
  );
  draw();
  return { el, refresh: draw };
}
