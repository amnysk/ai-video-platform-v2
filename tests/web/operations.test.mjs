import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { loadOperations, normalizeOperations, buildPath, availability } from '../../apps/web/static/js/operations.js';
import { repoPath } from './_py.mjs';

// 実物（backend が生成）を読む。コピーは持たない
const fixture = JSON.parse(readFileSync(repoPath('apps/web/static/operations.json'), 'utf8'));

test('実カタログを正規化する（path は prefix 込みのまま）', () => {
  const cat = normalizeOperations(fixture);
  assert.equal(cat.byName.size, 16);
  assert.equal(cat.byName.get('resume_production').path, '/v1/intervention/episodes/{episode_id}/resume');
  assert.equal(cat.byName.get('start_agentic_production').humanOnly, true);
  assert.equal(cat.byName.get('apply_approved_change').scope, 'apply');
  assert.equal(cat.apiPrefix, '/v1/intervention');
  assert.ok(cat.byName.get('list_episodes').queryFields.some((f) => f.name === 'before_id'));
  for (const spec of cat.byName.values()) assert.ok(spec.path.startsWith(cat.apiPrefix), spec.name);
});

test('カタログが無い・壊れていれば null（画面は縮退する）', async () => {
  assert.equal(await loadOperations(async () => ({ ok: false }), 'x'), null);
  assert.equal(await loadOperations(async () => { throw new Error('x'); }, 'x'), null);
  assert.equal(await loadOperations(async () => ({ ok: true, json: async () => ({}) }), 'x'), null);
});

test('buildPath は埋めて encode し、足りなければ throw', () => {
  const spec = normalizeOperations(fixture).byName.get('apply_approved_change');
  assert.equal(buildPath(spec, { episode_id: 'e', change_set_id: 'a/b' }), '/v1/intervention/episodes/e/change-sets/a%2Fb/apply');
  assert.throws(() => buildPath(spec, { episode_id: 'e' }));
});

test('availability: scope が分からなければ常に有効（権限判定はサーバー）', () => {
  const spec = { scope: 'apply' };
  assert.deepEqual(availability(spec, null), { enabled: true, reason: null });
  assert.equal(availability(spec, ['read']).enabled, false);
  assert.match(availability(spec, ['read']).reason, /apply/);
  assert.equal(availability(spec, ['apply']).enabled, true);
});

test('有料かどうかは operations.json の paid だけから決まる（JS に操作名の一覧を持たない）', () => {
  const cat = normalizeOperations(fixture);
  const fromJson = fixture.operations.filter((o) => o.paid === true).map((o) => o.name).sort();
  assert.deepEqual([...cat.byName.values()].filter((o) => o.paid).map((o) => o.name).sort(), fromJson);
  assert.deepEqual(fromJson, ['apply_approved_change', 'resume_production', 'start_agentic_production']);
  // フラグを書き換えると UI の判定がそのまま変わる（リテラルを見ていない証拠）
  const flipped = structuredClone(fixture);
  for (const o of flipped.operations) o.paid = o.name === 'request_hold';
  const cat2 = normalizeOperations(flipped);
  assert.deepEqual([...cat2.byName.values()].filter((o) => o.paid).map((o) => o.name), ['request_hold']);
});

test('paid が boolean でない操作は有料として扱い（fail closed）、壊れたカタログとして列挙する', () => {
  const broken = structuredClone(fixture);
  for (const o of broken.operations) {
    if (o.name === 'request_hold') delete o.paid; // 欠落
    if (o.name === 'get_storyboard') o.paid = 'false'; // boolean ではない
  }
  const cat = normalizeOperations(broken);
  assert.equal(cat.byName.get('request_hold').paid, true);
  assert.equal(cat.byName.get('get_storyboard').paid, true);
  assert.deepEqual([...cat.malformedPaid].sort(), ['get_storyboard', 'request_hold']);
  // 正常なカタログでは空、明示 false は false のまま
  const ok = normalizeOperations(fixture);
  assert.deepEqual(ok.malformedPaid, []);
  assert.equal(ok.byName.get('request_hold').paid, false);
});
