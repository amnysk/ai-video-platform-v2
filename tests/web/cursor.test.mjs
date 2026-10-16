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

test('さらに読み込むは、最後に検索ボタンを押したときの絞り込みを使う（入力欄の途中の値ではない）', async () => {
  const { createSearchFilters, buildListQuery } = await import('../../apps/web/static/js/cursor.js');
  const f = createSearchFilters();
  assert.deepEqual(f.current(), { status: '', q: '' });
  f.submit({ status: 'failed', q: ' cats ' });
  // 入力欄を書き換えても（submit していない）、current() は submit 時の値のまま
  assert.deepEqual(buildListQuery({ limit: 50, before: 'b', beforeId: 'i', ...f.current() }), {
    limit: 50, before: 'b', before_id: 'i', status: 'failed', q: 'cats',
  });
  // current() の戻り値を書き換えても内部状態は変わらない
  f.current().q = 'x';
  assert.equal(f.current().q, ' cats ');
  f.submit({ status: '', q: '' });
  assert.deepEqual(f.current(), { status: '', q: '' });
});
