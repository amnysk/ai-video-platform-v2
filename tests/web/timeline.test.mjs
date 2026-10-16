import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { buildAuditEntries, filterEntries, traceIdOf } from '../../apps/web/static/js/timeline.js';
import { repoPath } from './_py.mjs';

const rm = JSON.parse(readFileSync(repoPath('tests/web/fixtures/workspace.json'), 'utf8')).read_model;
const entries = buildAuditEntries(rm);
const field = (e, name) => e.fields.find((f) => f[0] === name)[1];

test('4種類を時刻の昇順に併合する', () => {
  assert.deepEqual([...new Set(entries.map((e) => e.kind))].sort(), ['change_set', 'command', 'decision', 'event']);
  const times = entries.map((e) => Date.parse(e.at));
  assert.deepEqual(times, [...times].sort((a, b) => a - b));
  assert.equal(entries.length, 2 + 2 + 2 + 1);
});

test('command は型・状態・actor・reject_reason を出し、payload/result 全文は出さない', () => {
  const c = entries.find((e) => e.kind === 'command' && field(e, 'command_id') === 'cmd1');
  assert.equal(c.actorId, 'bot');
  assert.equal(field(c, 'reject_reason'), '未記録');
  const dumped = JSON.stringify(entries);
  assert.ok(!dumped.includes('must-not-be-printed'));
  const pending = entries.find((e) => e.kind === 'command' && field(e, 'command_id') === 'cmd3');
  assert.match(field(pending, 'status'), /完了ではありません/);
});

test('command の reason / correlation_id は read model の項目から。無ければ未記録', () => {
  const c1 = entries.find((e) => e.kind === 'command' && field(e, 'command_id') === 'cmd1');
  assert.equal(field(c1, 'reason'), '確認のため');
  assert.equal(field(c1, 'correlation_id'), 'corr-cmd1');
  const c3 = entries.find((e) => e.kind === 'command' && field(e, 'command_id') === 'cmd3');
  assert.equal(field(c3, 'reason'), '未記録');
  assert.equal(field(c3, 'correlation_id'), '未記録');
});

test('trace_id / correlation_id は、フィールドがあるときだけ。無ければ未記録', () => {
  const ev1 = entries.find((e) => e.kind === 'event' && field(e, 'seq') === '1');
  assert.equal(field(ev1, 'trace_id'), 't-1');
  const ev2 = entries.find((e) => e.kind === 'event' && field(e, 'seq') === '2');
  assert.equal(field(ev2, 'trace_id'), '未記録');
  assert.equal(field(ev2, 'correlation_id'), '未記録');
  assert.equal(traceIdOf(null, {}), '未記録');
});

test('変更案は base / applied の revision 参照を持つ', () => {
  const cs = entries.find((e) => e.kind === 'change_set');
  assert.equal(field(cs, 'base_revision'), 'r-sb0');
  assert.equal(field(cs, 'applied_revision'), 'r-sb');
});

test('導出された判断はそう表示する', () => {
  const d = entries.find((e) => e.kind === 'decision' && e.title.includes('brand_new_type'));
  assert.match(field(d, 'derived'), /導出/);
});

test('actor / stage で絞り込める', () => {
  const humans = filterEntries(entries, { actor: 'human' });
  assert.ok(humans.length > 0 && humans.every((e) => e.actorGroup === 'human'));
  assert.ok(filterEntries(entries, { actor: 'ai' }).every((e) => e.actorType === 'ai_assistant'));
  assert.ok(filterEntries(entries, { actor: 'agent' }).every((e) => e.actorType === 'agent'));
  assert.ok(filterEntries(entries, { stage: 'storyboard' }).every((e) => e.stage === 'storyboard'));
  assert.ok(filterEntries(entries, { stage: '__none__' }).every((e) => e.stage === null));
  assert.equal(filterEntries(entries, {}).length, entries.length);
});

test('時刻が無いものは末尾に置き、時刻を捏造しない', () => {
  const e = buildAuditEntries({ events: [{ seq: 1, event_name: 'a', created_at: null, refs: {} }, { seq: 2, event_name: 'b', created_at: '2026-01-01T00:00:00Z', refs: {} }] });
  assert.equal(e[0].title, 'b');
  assert.equal(e[1].at, null);
});

test('空の read_model でも落ちない', () => {
  assert.deepEqual(buildAuditEntries({}), []);
  assert.deepEqual(buildAuditEntries(undefined), []);
});
