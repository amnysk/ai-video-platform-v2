import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { extractStages, STAGES, ARTIFACT_TYPE_STAGE, JOB_TYPE_STAGE, DECISION_TYPE_STAGE, jobsStatus } from '../../apps/web/static/js/stages.js';
import { pyEnumValues, repoPath } from './_py.mjs';

const ws = JSON.parse(readFileSync(repoPath('tests/web/fixtures/workspace.json'), 'utf8'));
const progress = JSON.parse(readFileSync(repoPath('tests/web/fixtures/progress.json'), 'utf8'));
const byId = (r, id) => r.stages.find((s) => s.id === id);

test('ステージは固定の7つ・順序どおり', () => {
  assert.deepEqual(STAGES.map((s) => s.label), ['Research', 'Strategy・Brief', 'Script', 'Storyboard', 'Production', 'Render', 'Upload']);
  assert.equal(extractStages(ws, null).stages.length, 7);
});

test('振り分け表のキーは実在する enum の値だけ', () => {
  const artifacts = pyEnumValues('contracts/states.py', 'ArtifactType');
  for (const k of Object.keys(ARTIFACT_TYPE_STAGE)) assert.ok(artifacts.includes(k), k);
  const jobs = pyEnumValues('contracts/states.py', 'JobType');
  for (const k of Object.keys(JOB_TYPE_STAGE)) assert.ok(jobs.includes(k), k);
  for (const k of Object.keys(JOB_TYPE_STAGE).concat(Object.keys(DECISION_TYPE_STAGE))) assert.ok(k);
  // 全 JobType（dummy を除く）が振り分けられている
  for (const j of jobs.filter((x) => x !== 'dummy')) assert.ok(JOB_TYPE_STAGE[j], j);
});

test('成果物・台本・evidence・判断・agent run・job が正しい stage に入る', () => {
  const r = extractStages(ws, progress);
  assert.equal(byId(r, 'script').members[0].slot, 'script');
  assert.equal(byId(r, 'script').script.title, '題名');
  assert.deepEqual(byId(r, 'script').inputs.map((i) => i.from), ['creative_brief']);
  assert.equal(byId(r, 'storyboard').members[0].stale, true);
  assert.equal(byId(r, 'research').evidence[0].verdict, 'pass');
  assert.equal(byId(r, 'strategy').decisions[0].type, 'choose_topic');
  assert.equal(byId(r, 'strategy').agentRuns[0].model, null); // 補完しない
  assert.equal(byId(r, 'script').agentRuns[0].model, 'gpt-x');
  assert.equal(byId(r, 'production').jobs[0].type, 'produce_scene_image');
});

test('未知の型は「ステージ未分類」に置かれ、捨てられない', () => {
  const r = extractStages(ws, progress);
  assert.equal(r.unclassified.members[0].slot, 'mystery');
  assert.equal(r.unclassified.decisions[0].type, 'brand_new_type');
});

test('判断の正規化: 候補・理由種別・不確実性が残り、欠落は null のまま', () => {
  const d = byId(extractStages(ws, null), 'strategy').decisions[0];
  assert.equal(d.rationaleKind, 'none');
  assert.equal(d.rationale, null);
  assert.equal(d.candidates.length, 2);
  assert.equal(d.candidates[1].rejectedReason, '弱い');
  assert.deepEqual(d.uncertainties, ['不明点']);
  assert.equal(d.evidenceCheck, null);
});

test('tool 名が無い invocation は name=null（捏造しない）', () => {
  const run = byId(extractStages(ws, null), 'strategy').agentRuns[0];
  assert.deepEqual(run.toolInvocations.map((t) => t.name), ['web_search', null]);
});

test('job 状態・失敗の帰属・復旧可否', () => {
  const r = extractStages(ws, progress);
  assert.equal(byId(r, 'script').jobStatus, 'succeeded');
  assert.equal(byId(r, 'production').jobStatus, 'retryable_failed');
  assert.equal(byId(r, 'render').jobStatus, 'terminal_failed');
  assert.equal(byId(r, 'production').failures[0].code, 'provider_rejected');
  assert.equal(r.episodeFailure, null);
  assert.ok(byId(r, 'production').recoverability.some((s) => s.includes('試行 2/3')));
  assert.ok(byId(r, 'render').recoverability.some((s) => s.includes('復旧は未実装')));
  assert.ok(byId(r, 'production').recoverability.some((s) => s.includes('自動では再送しません')));
});

test('帰属先が分からない失敗は Episode 全体の失敗として返す', () => {
  const p = { ...progress, failure: { code: 'x', message: null, source: 'command', source_id: 'c', at: null } };
  assert.equal(extractStages(ws, p).episodeFailure.code, 'x');
});

test('progress が無くても job 欄は空で、状態は null（未記録）', () => {
  const r = extractStages(ws, null);
  assert.equal(byId(r, 'render').jobStatus, null);
  assert.equal(jobsStatus([]), null);
});

test('空の workspace でも落ちない', () => {
  const r = extractStages({}, null);
  assert.equal(r.stages.length, 7);
  assert.equal(r.unclassified, null);
});
