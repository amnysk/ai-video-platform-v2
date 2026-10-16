// apps/web/static/operations.json（scripts/export-operations.py が生成。手で直さない）の読み込み。
// 実際の形: {api_prefix, catalog_version, operations:[{name, method, path（prefix 込み）, path_params,
//   required_scope, human_only, paid, effect, body_fields[{name,type,required,nullable,enum}], query_fields[...]}]}。
// `paid`（有料の外部生成を伴いうるか）もカタログ（contracts の OperationSpec.paid）が唯一の宣言元。JS に操作名の一覧を持たない。
// 無い・壊れているときは null を返し、画面は「操作カタログが無い」ことを示す（API の path を手書きしない）。

function normalize(raw) {
  if (!raw || typeof raw !== 'object' || !Array.isArray(raw.operations)) return null;
  const byName = new Map();
  const malformedPaid = []; // paid が boolean でない操作。有料として扱い（fail closed）、画面に警告を出す
  for (const o of raw.operations) {
    if (!o || typeof o.name !== 'string' || typeof o.path !== 'string') continue;
    if (typeof o.paid !== 'boolean') malformedPaid.push(o.name);
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
      paid: typeof o.paid === 'boolean' ? o.paid : true, // 欠落・不正は有料扱い（確認を増やす側に倒す）
    });
  }
  return { apiPrefix: raw.api_prefix ?? null, catalogVersion: raw.catalog_version ?? null, byName, malformedPaid };
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
