// apps/web/static/operations.json（backend が contracts/application_operations.py から生成）の読み込み。
// 形（想定）: {catalog_version, operations:[{name, method, path, scope, human_only,
//   request_fields, incurs_cost}]}。path は `/v1/intervention` を含む場合も含まない場合も受ける。
// 無い・壊れているときは null を返し、画面は「操作カタログが無い」ことを示す（API の path を手書きしない）。

export const API_PREFIX = '/v1/intervention';

function normalize(raw) {
  if (!raw || typeof raw !== 'object') return null;
  const list = Array.isArray(raw.operations)
    ? raw.operations
    : raw.operations && typeof raw.operations === 'object'
      ? Object.entries(raw.operations).map(([name, v]) => ({ name, ...v }))
      : null;
  if (!list) return null;
  const byName = new Map();
  for (const o of list) {
    if (!o || typeof o.name !== 'string' || typeof o.path !== 'string') continue;
    const path = o.path.startsWith(API_PREFIX) ? o.path : API_PREFIX + o.path;
    byName.set(o.name, {
      name: o.name,
      method: String(o.method || 'GET').toUpperCase(),
      path,
      scope: o.scope ?? null,
      humanOnly: Boolean(o.human_only ?? o.humanOnly),
      requestFields: Array.isArray(o.request_fields) ? o.request_fields : [],
      incursCost: Boolean(o.incurs_cost ?? o.cost ?? o.costly),
    });
  }
  return { catalogVersion: raw.catalog_version ?? null, byName };
}

export async function loadOperations(fetchFn, url = './operations.json') {
  try {
    const res = await fetchFn(url, { headers: { Accept: 'application/json' } });
    if (!res.ok) return null;
    return normalize(await res.json());
  } catch {
    return null;
  }
}

export { normalize as normalizeOperations };

/** path テンプレートの `{name}` を埋める。値は encodeURIComponent。足りなければ throw。 */
export function buildPath(spec, params = {}) {
  return spec.path.replace(/\{(\w+)\}/g, (_, key) => {
    const v = params[key];
    if (v === undefined || v === null || v === '') throw new Error(`path parameter missing: ${key}`);
    return encodeURIComponent(String(v));
  });
}

/**
 * ボタンの可否。principal の scope は API から知る手段が無い（knownScopes=null）ので、
 * 通常は常に {enabled:true, reason:null}。サーバーが権限の唯一の判定者であり、これは安全装置ではない。
 */
export function availability(spec, knownScopes) {
  if (!knownScopes) return { enabled: true, reason: null };
  if (spec.scope && !knownScopes.includes(spec.scope)) {
    return { enabled: false, reason: `この操作には scope「${spec.scope}」が必要です` };
  }
  return { enabled: true, reason: null };
}
