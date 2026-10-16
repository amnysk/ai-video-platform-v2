import test from 'node:test';
import assert from 'node:assert/strict';
import { parseHash, detailHref } from '../../apps/web/static/js/router.js';

const ID = '0b6f1a42-3c9e-4d7a-8f10-a1b2c3d4e5f6';

test('一覧', () => {
  for (const h of ['', '#', '#/', '#/?q=a']) assert.deepEqual(parseHash(h), { name: 'list' });
});
test('詳細は UUID だけ受ける', () => {
  assert.deepEqual(parseHash(`#/episodes/${ID}`), { name: 'detail', episodeId: ID });
  assert.deepEqual(parseHash(`#/episodes/${ID}/`), { name: 'detail', episodeId: ID });
  assert.equal(parseHash('#/episodes/not-a-uuid').name, 'notfound');
  assert.equal(parseHash(`#/episodes/${ID}/../x`).name, 'notfound');
  assert.equal(parseHash('#/other').name, 'notfound');
  assert.equal(detailHref(ID), `#/episodes/${ID}`);
});
