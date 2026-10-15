import test from 'node:test';
import assert from 'node:assert/strict';
import { orMissing, formatDateTime, formatUsd, briefValue, MISSING, COST_NOT_ESTIMATED } from '../../apps/web/static/js/format.js';

test('orMissing: null/空だけ未記録。0 と false は記録された値', () => {
  assert.equal(orMissing(null), MISSING);
  assert.equal(orMissing(undefined), MISSING);
  assert.equal(orMissing(''), MISSING);
  assert.equal(orMissing(0), '0');
  assert.equal(orMissing(false), 'false');
});
test('費用 null は 0 ではなく未見積り', () => {
  assert.equal(formatUsd(null), COST_NOT_ESTIMATED);
  assert.equal(formatUsd('0.0000'), '$0.0000');
});
test('日時: null は未記録、不正値はそのまま', () => {
  assert.equal(formatDateTime(null), MISSING);
  assert.equal(formatDateTime('not-a-date'), 'not-a-date');
  assert.match(formatDateTime('2026-10-01T00:00:00+00:00'), /2026/);
});
test('briefValue はオブジェクトを展開しない', () => {
  assert.equal(briefValue({ secret: 'x' }), '{…}');
  assert.equal(briefValue('a'.repeat(200)).length, 121);
});
