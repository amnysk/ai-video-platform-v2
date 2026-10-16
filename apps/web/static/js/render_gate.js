// main.js の render() が await を挟んでも、古い render が新しい画面を上書きしない・view（と poller）を漏らさないための世代管理。DOM 無し。
export function createRenderGate() {
  let generation = 0;
  let current = null;
  return {
    /** render の開始。await の後に isCurrent() を確認し、false なら何もせず戻る。 */
    begin() {
      const mine = ++generation;
      return { isCurrent: () => mine === generation };
    },
    /** 現在の view を破棄する（画面を離れる・作り直す前）。 */
    disposeCurrent() {
      const view = current;
      current = null;
      view?.dispose?.();
    },
    /** view を現在の画面にする。token が古ければ view を破棄して false。直前の view は必ず破棄する。 */
    install(token, view) {
      if (!token.isCurrent()) {
        view?.dispose?.();
        return false;
      }
      this.disposeCurrent();
      current = view;
      return true;
    },
  };
}
