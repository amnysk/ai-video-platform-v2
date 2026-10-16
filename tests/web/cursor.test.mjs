import test from 'node:test';
import assert from 'node:assert/strict';
import { buildListQuery, nextCursor, mergePage } from '../../apps/web/static/js/cursor.js';

test('buildListQuery: before_id は before とセットのときだけ', () => {
  assert.deepEqual(buildListQuery({ limit: 20 }), { limit: 20 });
  assert.deepEqual(buildListQuery({ before: 't', beforeId: 'i', status: 'failed', q: ' x ' }), {
    limit: 50, before: 't', before_id: 'i', status: 'failed', q: 'x',
  });
  assert.equal('before_id' in buildListQuery({ beforeId: 'i' }), false);
});

test('nextCursor: 複合 / degraded / 終端', () => {
  assert.deepEqual(nextCursor({ next_before: 't', next_before_id: 'i' }), { before: 't', beforeId: 'i', degraded: false });
  assert.deepEqual(nextCursor({ next_before: 't' }), { before: 't', beforeId: null, degraded: true });
  assert.equal(nextCursor({ next_before: null }), null);
  assert.equal(nextCursor(null), null);
});

test('mergePage: id で重複排除し順序を保つ', () => {
  const a = [{ episode_id: '1' }, { episode_id: '2' }];
  const r = mergePage(a, { episodes: [{ episode_id: '2' }, { episode_id: '3' }, { episode_id: '3' }] });
  assert.deepEqual(r.items.map((e) => e.episode_id), ['1', '2', '3']);
  assert.equal(r.added, 1);
  assert.equal(a.length, 2); // 引数を破壊しない
});
