import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { loadOperations, normalizeOperations, buildPath, availability } from '../../apps/web/static/js/operations.js';
import { repoPath } from './_py.mjs';

const fixture = JSON.parse(readFileSync(repoPath('tests/web/fixtures/operations.json'), 'utf8'));

test('fixture を正規化し、path に prefix を補う', () => {
  const cat = normalizeOperations(fixture);
  assert.equal(cat.byName.size, 13);
  assert.equal(cat.byName.get('resume_production').path, '/v1/intervention/episodes/{episode_id}/resume');
  assert.equal(cat.byName.get('start_agentic_production').humanOnly, true);
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
