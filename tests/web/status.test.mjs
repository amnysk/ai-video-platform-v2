import test from 'node:test';
import assert from 'node:assert/strict';
import {
  EPISODE_STATUS_LABEL,
  NEEDS_ATTENTION,
  needsAttention,
  episodeStatusLabel,
  pipelineKindLabel,
  JOB_STATUS_LABEL,
  COMMAND_STATUS_LABEL,
  ACTOR_TYPE_LABEL,
} from '../../apps/web/static/js/status.js';
import { pyEnumValues } from './_py.mjs';

const episodeStatuses = pyEnumValues('contracts/states.py', 'EpisodeStatus');

test('EpisodeStatus の全値にラベルがあり、余計な値は無い', () => {
  assert.deepEqual(Object.keys(EPISODE_STATUS_LABEL).sort(), [...episodeStatuses].sort());
});

test('要注意は contracts/states.py の EpisodeStatus の部分集合だけ', () => {
  for (const k of Object.keys(NEEDS_ATTENTION)) assert.ok(episodeStatuses.includes(k), k);
  assert.equal(needsAttention('failed'), '失敗');
  assert.equal(needsAttention('ready_for_review'), '確認待ち');
  assert.equal(needsAttention('in_progress'), null);
  assert.equal(needsAttention('totally_unknown'), null); // 推測で要注意にしない
  assert.equal(needsAttention('toString'), null); // prototype 汚染的な名前
});

test('未知の状態は未知と表示する', () => {
  assert.equal(episodeStatusLabel('zzz'), '未知の状態');
  assert.equal(pipelineKindLabel('agentic'), 'Agentic');
  assert.equal(pipelineKindLabel('legacy'), 'Legacy');
  assert.equal(pipelineKindLabel(undefined), '未記録');
});

test('Job / Command / Actor のラベルは Python の enum と一致する', () => {
  assert.deepEqual(Object.keys(JOB_STATUS_LABEL).sort(), pyEnumValues('contracts/states.py', 'JobStatus').sort());
  assert.deepEqual(Object.keys(COMMAND_STATUS_LABEL).sort(), pyEnumValues('contracts/intervention.py', 'CommandStatus').sort());
  assert.deepEqual(Object.keys(ACTOR_TYPE_LABEL).sort(), pyEnumValues('contracts/intervention.py', 'ActorType').sort());
});
