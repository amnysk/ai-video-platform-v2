// 経過表示の polling。タブが前面のときだけ動き、重なって走らず、stop() で完全に止まる。DOM 無し。

export function startPoller({
  fn,
  intervalMs = 10_000,
  isVisible,
  setTimer = setTimeout,
  clearTimer = clearTimeout,
}) {
  let stopped = false;
  let timer = null;
  let running = false;

  const schedule = () => {
    if (stopped) return;
    timer = setTimer(tick, intervalMs);
  };

  async function tick() {
    timer = null;
    if (stopped) return;
    if (isVisible() && !running) {
      running = true;
      try {
        await fn();
      } catch {
        // 失敗の表示は fn 側の責務。polling は続ける
      } finally {
        running = false;
      }
    }
    schedule();
  }

  schedule();
  return {
    /** タブが前面に戻ったとき、次の周期を待たずに1回読む。 */
    pokeIfVisible() {
      if (stopped || running || !isVisible()) return;
      if (timer !== null) clearTimer(timer);
      void tick();
    },
    stop() {
      stopped = true;
      if (timer !== null) clearTimer(timer);
      timer = null;
    },
  };
}
