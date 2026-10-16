import test from 'node:test';
import assert from 'node:assert/strict';
import { ERROR_INFO, ApiError, describeError, validationLines } from '../../apps/web/static/js/errors.js';
import { pyEnumValues } from './_py.mjs';

test('OperationErrorCode の全値に日本語の理由と対処がある', () => {
  const codes = pyEnumValues('contracts/application.py', 'OperationErrorCode');
  assert.ok(codes.length >= 15);
  for (const c of codes) {
    assert.ok(ERROR_INFO[c], `missing mapping for ${c}`);
    assert.match(ERROR_INFO[c].reason, /[぀-ヿ一-鿿]/);
    assert.ok(ERROR_INFO[c].action);
  }
});

test('RejectReason の全値にも説明がある', () => {
  for (const c of pyEnumValues('contracts/intervention.py', 'RejectReason')) {
    assert.ok(ERROR_INFO[c], `missing mapping for ${c}`);
  }
});

test('stale は reload を要求する', () => {
  for (const code of ['stale_control_revision', 'stale_target_revision']) {
    const d = describeError(new ApiError({ status: 409, code, correlationId: 'c1' }));
    assert.equal(d.reload, true);
    assert.equal(d.correlationId, 'c1');
  }
});

test('HTTP 分類: network / 401 / 422 / 5xx', () => {
  assert.equal(describeError(new ApiError({ status: 0 })).retry, true);
  assert.equal(describeError(new ApiError({ status: 0 })).code, 'network_error');
  assert.equal(describeError(new ApiError({ status: 401 })).code, 'unauthorized');
  assert.equal(describeError(new ApiError({ status: 422, detail: [] })).code, 'validation');
  assert.equal(describeError(new ApiError({ status: 503 })).retry, true);
});

test('未知の code でも落ちず、code をそのまま見せる', () => {
  const d = describeError(new ApiError({ status: 422, code: 'brand_new_code' }));
  assert.equal(d.code, 'brand_new_code');
  assert.ok(d.reason);
});

test('Error 以外が来ても describeError は落ちない', () => {
  assert.ok(describeError(new Error('x')).reason);
});

test('validationLines は loc と msg を短く返す', () => {
  const lines = validationLines([{ loc: ['body', 'proposal', 'edits'], msg: 'too short' }]);
  assert.deepEqual(lines, ['proposal.edits: too short']);
  assert.deepEqual(validationLines('x'), []);
});

test('408 / 425 / 429 は一時的（同じ操作の再試行で成功しうる）として扱う', () => {
  for (const status of [408, 425, 429]) {
    const d = describeError(new ApiError({ status }));
    assert.equal(d.retry, true, String(status));
    assert.equal(d.reload, false, String(status));
    assert.notEqual(d.code, 'unknown', String(status));
  }
  // 確定的な 4xx は再試行扱いにしない
  for (const status of [400, 403, 422]) assert.equal(describeError(new ApiError({ status })).retry, false, String(status));
});
