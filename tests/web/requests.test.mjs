import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import {
  buildHoldBody, buildResumeBody, buildProposeBody, buildApplyBody, validateApply, validateProposal,
  validateHold, verifyEpisode, LEGACY_START_STAGES, VISUAL_DESCRIPTION_MAX,
} from '../../apps/web/static/js/requests.js';
import { normalizeOperations } from '../../apps/web/static/js/operations.js';
import { pyEnumValues, repoPath } from './_py.mjs';

const cat = normalizeOperations(JSON.parse(readFileSync(repoPath('tests/web/fixtures/operations.json'), 'utf8')));
const fields = (n) => cat.byName.get(n).requestFields.slice().sort();
const keys = (o) => Object.keys(o).sort();
const sb = { manifest_id: 'm', revision_id: 'r' };

test('body のキーは operations の request_fields と一致する', () => {
  assert.deepEqual(keys(buildHoldBody({ commandId: 'c', expectedControlRevision: 1 })), fields('request_hold'));
  assert.deepEqual(keys(buildResumeBody({ commandId: 'c', expectedControlRevision: 1 })), fields('resume_production'));
  assert.deepEqual(keys(buildApplyBody({ commandId: 'c', expectedControlRevision: 1, input: '1' })), fields('apply_approved_change'));
  const p = buildProposeBody({ commandId: 'c', expectedControlRevision: 1, episodeId: 'e', storyboard: sb, sceneId: 'sb1', text: 'x' });
  assert.deepEqual(keys(p), fields('propose_storyboard_change'));
});

test('提案は visual_description だけを送る', () => {
  const p = buildProposeBody({ commandId: 'c', expectedControlRevision: 2, episodeId: 'e', storyboard: sb, sceneId: 'sb1', text: ' 新 ', rationale: ' ' });
  assert.deepEqual(p.proposal.edits, [{ scene_id: 'sb1', visual_description: '新' }]);
  assert.equal(p.proposal.rationale, null);
  assert.equal(p.proposal.base_storyboard_revision_id, 'r');
});

test('legacy の start_stage は PipelineStage の値と一致する', () => {
  assert.deepEqual(LEGACY_START_STAGES, pyEnumValues('contracts/pipeline.py', 'PipelineStage'));
});

test('visual_description の上限は contracts と同じ', () => {
  const src = readFileSync(repoPath('contracts/intervention.py'), 'utf8');
  assert.ok(src.includes(`visual_description: str = Field(min_length=1, max_length=${VISUAL_DESCRIPTION_MAX})`));
});

test('validateProposal', () => {
  assert.match(validateProposal({ sceneId: '', text: 'x' }), /シーン/);
  assert.match(validateProposal({ sceneId: 's', original: 'a', text: ' a ' }), /同じ/);
  assert.match(validateProposal({ sceneId: 's', original: 'a', text: 'x'.repeat(601) }), /600/);
  assert.equal(validateProposal({ sceneId: 's', original: 'a', text: 'b' }), null);
});

test('validateHold', () => {
  assert.equal(validateHold({ reason: 'x' }), null);
  assert.ok(validateHold({ reason: 'x'.repeat(1001) }));
});

const preview = (o = {}) => ({ status: 'proposed', estimated_cost_usd: '1.5000', cost_estimated: true, blockers: [], ...o });

test('validateApply: 見積り null / 未見積り / blocker / 空入力 / 未満 は送らない', () => {
  assert.match(validateApply({ preview: preview({ estimated_cost_usd: null, cost_estimated: false }), input: '5' }), /見積もられていない/);
  assert.match(validateApply({ preview: preview({ cost_estimated: false }), input: '5' }), /見積もられていない/);
  assert.match(validateApply({ preview: preview({ blockers: ['invalid_state'] }), input: '5' }), /blocker/);
  assert.match(validateApply({ preview: preview(), input: '' }), /入力/);
  assert.match(validateApply({ preview: preview(), input: 'abc', acknowledged: true }), /数値/);
  assert.match(validateApply({ preview: preview(), input: '1', acknowledged: true }), /以上/);
  assert.match(validateApply({ preview: preview(), input: '2' }), /確認/);
  assert.match(validateApply({ preview: preview({ status: 'applied' }), input: '2', acknowledged: true }), /適用できる状態/);
  assert.equal(validateApply({ preview: preview(), input: '2', acknowledged: true }), null);
  assert.equal(validateApply({ preview: preview({ estimated_cost_usd: '0.0000' }), input: '0' }), null);
  assert.match(validateApply({ preview: null, input: '1' }), /読み込めて/);
});

test('apply body の上限は数値', () => {
  assert.equal(buildApplyBody({ commandId: 'c', expectedControlRevision: 3, input: ' 2.5 ' }).approved_max_cost_usd, 2.5);
});

test('verifyEpisode: route と workspace の Episode が違えば中止', () => {
  const ws = { episode: { episode_id: 'AAA' } };
  assert.equal(verifyEpisode('aaa', ws), null);
  assert.match(verifyEpisode('bbb', ws), /一致しません/);
  assert.match(verifyEpisode('aaa', null), /一致しません/);
});
