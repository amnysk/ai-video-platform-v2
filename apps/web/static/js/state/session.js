// トークンの保持。sessionStorage だけ（タブを閉じると消える）。localStorage・cookie は使わない。
// トークンをログ・DOM・URL に出さない。storage が使えない環境ではメモリだけに保持する。

const KEY = 'jpmas2.operator.token';

export function createSession(storage = globalThis.sessionStorage) {
  let memory = null;
  const safe = (fn) => {
    try {
      return fn();
    } catch {
      return undefined;
    }
  };
  return {
    getToken() {
      const v = storage ? safe(() => storage.getItem(KEY)) : undefined;
      return v ?? memory;
    },
    setToken(token) {
      const t = String(token ?? '').trim();
      if (!t) return false;
      memory = t;
      if (storage) safe(() => storage.setItem(KEY, t));
      return true;
    },
    clear() {
      memory = null;
      if (storage) safe(() => storage.removeItem(KEY));
    },
    hasToken() {
      return Boolean(this.getToken());
    },
  };
}
