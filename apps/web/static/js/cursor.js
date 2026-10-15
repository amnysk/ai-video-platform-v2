// 一覧のページング。複合 cursor（before + before_id）と、id による重複排除。DOM 無し。

export function buildListQuery({ limit = 50, before = null, beforeId = null, status = '', q = '' } = {}) {
  const query = { limit };
  if (before) query.before = before;
  // before_id は before とセットでだけ意味がある
  if (before && beforeId) query.before_id = beforeId;
  if (status) query.status = status;
  const term = (q || '').trim();
  if (term) query.q = term;
  return query;
}

/** 応答から次ページの cursor。無ければ null（最後のページ）。
 *  next_before_id が無い（backend が複合 cursor 未対応）ときは degraded=true で before のみ。 */
export function nextCursor(page) {
  if (!page || !page.next_before) return null;
  const beforeId = page.next_before_id ?? null;
  return { before: page.next_before, beforeId, degraded: beforeId === null };
}

/** 既存の行に次のページを足す。episode_id で重複を除き、順序は到着順を保つ。 */
export function mergePage(existing, page) {
  const seen = new Set(existing.map((e) => e.episode_id));
  const items = existing.slice();
  let added = 0;
  for (const ep of page?.episodes ?? []) {
    if (seen.has(ep.episode_id)) continue;
    seen.add(ep.episode_id);
    items.push(ep);
    added += 1;
  }
  return { items, added };
}
