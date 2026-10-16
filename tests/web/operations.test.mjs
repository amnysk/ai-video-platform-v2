import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { loadOperations, normalizeOperations, buildPath, availability, PAID_OPERATIONS } from '../../apps/web/static/js/operations.js';
import { repoPath } from './_py.mjs';

// 実物（backend が生成）を読む。コピーは持たない
const fixture = JSON.parse(readFileSync(repoPath('apps/web/static/operations.json'), 'utf8'));

test('実カタログを正規化する（path は prefix 込みのまま）', () => {
  const cat = normalizeOperations(fixture);
  assert.equal(cat.byName.size, 13);
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

test('有料操作は ADR-0044 の 2 つで、実カタログに実在する（カタログに費用項目は無い）', () => {
  const cat = normalizeOperations(fixture);
  assert.deepEqual([...PAID_OPERATIONS].sort(), ['apply_approved_change', 'start_agentic_production']);
  for (const n of PAID_OPERATIONS) assert.ok(cat.byName.has(n), n);
  assert.deepEqual(
    [...cat.byName.values()].filter((o) => o.paid).map((o) => o.name).sort(),
    [...PAID_OPERATIONS].sort(),
  );
  for (const o of fixture.operations) {
    assert.ok(!Object.keys(o).some((k) => /cost|paid/.test(k)), `${o.name}: カタログに費用項目が増えたら PAID_OPERATIONS の持ち方を見直す`);
  }
});
