import test from 'node:test';
import assert from 'node:assert/strict';
import { createRenderGate } from '../../apps/web/static/js/render_gate.js';
import { startPoller } from '../../apps/web/static/js/poller.js';

function fakeTimers() {
  const q = new Map();
  let n = 0;
  return {
    setTimer: (f) => { q.set(++n, f); return n; },
    clearTimer: (id) => q.delete(id),
    size: () => q.size,
  };
}

/** main.js の render() と同じ形: begin → await → 古ければ捨てる → install。 */
function makeRender(gate, timers, wait) {
  const views = [];
  async function render(name) {
    const token = gate.begin();
    gate.disposeCurrent();
    await wait(name);
    if (!token.isCurrent()) return 'stale';
    const poller = startPoller({ fn: async () => {}, isVisible: () => true, ...timers });
    const view = { name, disposed: false, dispose() { this.disposed = true; poller.stop(); } };
    views.push(view);
    return gate.install(token, view) ? 'installed' : 'stale';
  }
  return { render, views };
}

test('遅い render が後から終わっても、新しい view を上書きせず、poller も残らない', async () => {
  const gate = createRenderGate();
  const timers = fakeTimers();
  const releases = {};
  const { render, views } = makeRender(gate, timers, (name) => new Promise((r) => { releases[name] = r; }));
  const a = render('A'); // 先に始まるが遅い
  const b = render('B'); // 後から始まり、先に終わる
  releases.B();
  assert.equal(await b, 'installed');
  releases.A();
  assert.equal(await a, 'stale');
  assert.deepEqual(views.map((v) => v.name), ['B'], 'A の view は作られない');
  assert.equal(timers.size(), 1, 'poller は B の 1 本だけ');
  gate.disposeCurrent();
  assert.equal(timers.size(), 0, '画面を離れたら poller は 0 本');
  assert.equal(views[0].disposed, true);
});

test('install は直前の view を必ず dispose する（render が dispose し忘れても漏れない）', () => {
  const gate = createRenderGate();
  const log = [];
  const t1 = gate.begin();
  gate.install(t1, { dispose: () => log.push('v1') });
  const t2 = gate.begin();
  assert.equal(gate.install(t2, { dispose: () => log.push('v2') }), true);
  assert.deepEqual(log, ['v1']);
});

test('古い token の view は install されず、その場で dispose される', () => {
  const gate = createRenderGate();
  const old = gate.begin();
  gate.begin();
  let disposed = false;
  assert.equal(gate.install(old, { dispose: () => { disposed = true; } }), false);
  assert.equal(disposed, true);
});
