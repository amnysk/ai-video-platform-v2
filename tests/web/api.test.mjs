import test from 'node:test';
import assert from 'node:assert/strict';
import { createApi } from '../../apps/web/static/js/api.js';
import { createSession } from '../../apps/web/static/js/state/session.js';

function memStorage() {
  const m = new Map();
  return { getItem: (k) => m.get(k) ?? null, setItem: (k, v) => m.set(k, v), removeItem: (k) => m.delete(k), _m: m };
}
function res(status, body, headers = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (k) => headers[k] ?? null },
    text: async () => (body === undefined ? '' : JSON.stringify(body)),
  };
}

test('Bearer と X-Correlation-ID を付け、相関 ID を返す', async () => {
  const session = createSession(memStorage());
  session.setToken('tok');
  let seen;
  const api = createApi({ session, uuid: () => 'cid-1', fetchFn: async (u, init) => { seen = { u, init }; return res(200, { a: 1 }); } });
  const r = await api.get('/v1/x', { query: { limit: 5, q: '' } });
  assert.equal(seen.u, '/v1/x?limit=5');
  assert.equal(seen.init.headers.Authorization, 'Bearer tok');
  assert.equal(seen.init.headers['X-Correlation-ID'], 'cid-1');
  assert.equal(r.correlationId, 'cid-1');
});

test('401 でトークンを消し onUnauthorized を呼ぶ', async () => {
  const st = memStorage();
  const session = createSession(st);
  session.setToken('tok');
  let called = 0;
  const api = createApi({ session, onUnauthorized: () => called++, fetchFn: async () => res(401, { detail: 'no' }) });
  await assert.rejects(api.get('/v1/x'), (e) => e.status === 401);
  assert.equal(session.hasToken(), false);
  assert.equal(st._m.size, 0);
  assert.equal(called, 1);
});

test('エラー本文の error / message を ApiError に写す。相関 ID は付く', async () => {
  const session = createSession(memStorage());
  const api = createApi({ session, uuid: () => 'c9', fetchFn: async () => res(409, { detail: { error: 'stale_control_revision', message: 'm' } }) });
  await assert.rejects(api.post('/v1/y', { a: 1 }), (e) => e.code === 'stale_control_revision' && e.serverMessage === 'm' && e.correlationId === 'c9');
});

test('通信失敗は status 0 の ApiError', async () => {
  const api = createApi({ session: createSession(memStorage()), fetchFn: async () => { throw new TypeError('x'); } });
  await assert.rejects(api.get('/v1/x'), (e) => e.status === 0);
});

test('POST は JSON body と Content-Type。同じ correlationId を再利用できる', async () => {
  let seen;
  const api = createApi({ session: createSession(memStorage()), fetchFn: async (u, init) => { seen = init; return res(202, {}); } });
  await api.post('/v1/z', { command_id: 'abc' }, { correlationId: 'fixed' });
  assert.equal(seen.headers['Content-Type'], 'application/json');
  assert.equal(seen.body, '{"command_id":"abc"}');
  assert.equal(seen.headers['X-Correlation-ID'], 'fixed');
});

test('If-None-Match / ETag は使わない（常に新しい本文を読む。古い本文を 304 で返さない）', async () => {
  const seen = [];
  const api = createApi({
    session: createSession(memStorage()),
    fetchFn: async (u, init) => { seen.push(init.headers['If-None-Match']); return res(200, { n: seen.length }, { ETag: '"e1"' }); },
  });
  await api.get('/v1/p');
  const r = await api.get('/v1/p');
  assert.deepEqual(seen, [undefined, undefined]);
  assert.deepEqual(r.data, { n: 2 });
});

test('session: sessionStorage だけを使い、空トークンは保存しない', () => {
  const st = memStorage();
  const s = createSession(st);
  assert.equal(s.setToken('  '), false);
  assert.equal(s.setToken(' abc '), true);
  assert.equal(s.getToken(), 'abc');
  assert.equal(createSession(st).getToken(), 'abc');
  s.clear();
  assert.equal(createSession(st).getToken(), null);
});

test('session: storage が壊れていてもメモリで動く', () => {
  const bad = { getItem() { throw new Error('x'); }, setItem() { throw new Error('x'); }, removeItem() { throw new Error('x'); } };
  const s = createSession(bad);
  s.setToken('t');
  assert.equal(s.getToken(), 't');
  s.clear();
  assert.equal(s.getToken(), null);
});
