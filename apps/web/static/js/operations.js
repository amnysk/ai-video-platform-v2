// apps/web/static/operations.json（scripts/export-operations.py が生成。手で直さない）の読み込み。
// 実際の形: {api_prefix, catalog_version, operations:[{name, method, path（prefix 込み）, path_params,
//   required_scope, human_only, effect, body_fields[{name,type,required,nullable,enum}], query_fields[...]}]}。
// カタログに「費用が発生するか」の項目は無い。ここでは推測せず、PAID_OPERATIONS（ADR-0044）だけを持つ。
// 無い・壊れているときは null を返し、画面は「操作カタログが無い」ことを示す（API の path を手書きしない）。

/** ADR-0044: 有料の外部生成を伴う操作は、この 2 つだけ。UI の費用警告はこれと影響プレビューの見積りだけから出す。 */
export const PAID_OPERATIONS = Object.freeze(['start_agentic_production', 'apply_approved_change']);

function normalize(raw) {
  if (!raw || typeof raw !== 'object' || !Array.isArray(raw.operations)) return null;
  const byName = new Map();
  for (const o of raw.operations) {
    if (!o || typeof o.name !== 'string' || typeof o.path !== 'string') continue;
    byName.set(o.name, {
      name: o.name,
      method: String(o.method || 'GET').toUpperCase(),
      path: o.path,
      pathParams: Array.isArray(o.path_params) ? o.path_params : [],
      scope: o.required_scope ?? null,
      humanOnly: o.human_only === true,
      effect: o.effect ?? null,
      bodyFields: Array.isArray(o.body_fields) ? o.body_fields : [],
      queryFields: Array.isArray(o.query_fields) ? o.query_fields : [],
      paid: PAID_OPERATIONS.includes(o.name),
    });
  }
  return { apiPrefix: raw.api_prefix ?? null, catalogVersion: raw.catalog_version ?? null, byName };
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
