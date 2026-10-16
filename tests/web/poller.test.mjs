import test from 'node:test';
import assert from 'node:assert/strict';
import { startPoller } from '../../apps/web/static/js/poller.js';

function fakeTimers() {
  const q = new Map();
  let n = 0;
  return {
    setTimer: (f) => { q.set(++n, f); return n; },
    clearTimer: (id) => q.delete(id),
    size: () => q.size,
    async fire() { const [id, f] = [...q][0]; q.delete(id); await f(); },
  };
}

test('前面のときだけ読む。非表示の周期は読まずに次を予約する', async () => {
  const t = fakeTimers();
  let visible = true;
  let calls = 0;
  const p = startPoller({ fn: async () => { calls++; }, isVisible: () => visible, ...t });
  await t.fire();
  assert.equal(calls, 1);
  visible = false;
  await t.fire();
  assert.equal(calls, 1);
  assert.equal(t.size(), 1); // 次の周期は予約されている
  p.stop();
});

test('stop 後は何も予約せず、読まない', async () => {
  const t = fakeTimers();
  let calls = 0;
  const p = startPoller({ fn: async () => { calls++; }, isVisible: () => true, ...t });
  p.stop();
  assert.equal(t.size(), 0);
  p.pokeIfVisible();
  assert.equal(calls, 0);
});

test('fn が失敗しても polling は続く', async () => {
  const t = fakeTimers();
  const p = startPoller({ fn: async () => { throw new Error('x'); }, isVisible: () => true, ...t });
  await t.fire();
  assert.equal(t.size(), 1);
  p.stop();
});

test('pokeIfVisible は前面のときだけ即時に1回読む', async () => {
  const t = fakeTimers();
  let visible = false;
  let calls = 0;
  const p = startPoller({ fn: async () => { calls++; }, isVisible: () => visible, ...t });
  p.pokeIfVisible();
  assert.equal(calls, 0);
  visible = true;
  p.pokeIfVisible();
  await Promise.resolve();
  assert.equal(calls, 1);
  p.stop();
});
