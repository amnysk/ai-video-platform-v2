// /v1/intervention への fetch。Bearer・X-Correlation-ID の付与、エラーの正規化、401 でトークン破棄。
// fetch / uuid は注入できる（node --test で検査する）。トークンはヘッダにだけ載せ、どこにも出力しない。
import { ApiError } from './errors.js';

export const CORRELATION_HEADER = 'X-Correlation-ID';

function makeUuid() {
  return globalThis.crypto.randomUUID();
}

export function createApi({ session, fetchFn = (...a) => globalThis.fetch(...a), uuid = makeUuid, onUnauthorized = () => {} }) {
  function qs(query) {
    const p = new URLSearchParams();
    for (const [k, v] of Object.entries(query ?? {})) {
      if (v !== undefined && v !== null && v !== '') p.set(k, String(v));
    }
    const s = p.toString();
    return s ? `?${s}` : '';
  }

  async function request(method, path, { query, body, signal, correlationId } = {}) {
    const cid = correlationId ?? uuid();
    const url = path + qs(query);
    const headers = { Accept: 'application/json', [CORRELATION_HEADER]: cid };
    const token = session.getToken();
    if (token) headers.Authorization = `Bearer ${token}`;
    const init = { method, headers, signal };
    if (body !== undefined) {
      headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }

    let res;
    try {
      res = await fetchFn(url, init);
    } catch (e) {
      if (e?.name === 'AbortError') throw e;
      throw new ApiError({ status: 0, correlationId: cid });
    }
    const echoed = res.headers?.get?.(CORRELATION_HEADER) ?? null;
    const shownCid = echoed || cid;

    let data = null;
    try {
      const text = await res.text();
      data = text ? JSON.parse(text) : null;
    } catch {
      data = null;
    }

    if (!res.ok) {
      const d = data?.detail;
      const code = d && typeof d === 'object' && !Array.isArray(d) ? d.error ?? null : null;
      const message = d && typeof d === 'object' && !Array.isArray(d) ? d.message ?? null : typeof d === 'string' ? d : null;
      if (res.status === 401) {
        session.clear();
        onUnauthorized();
      }
      throw new ApiError({ status: res.status, code, serverMessage: message, detail: d ?? null, correlationId: shownCid });
    }

    return { data, status: res.status, correlationId: shownCid };
  }

  return {
    get: (path, opts) => request('GET', path, opts),
    post: (path, body, opts) => request('POST', path, { ...opts, body }),
    request,
  };
}
