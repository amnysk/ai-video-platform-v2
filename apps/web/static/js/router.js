// hash routing。`#/` と `#/episodes/{uuid}` だけ。DOM 無し。
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function parseHash(hash) {
  const h = (hash || '').replace(/^#/, '') || '/';
  const path = h.split('?')[0].replace(/\/+$/, '') || '/';
  if (path === '/') return { name: 'list' };
  const m = /^\/episodes\/([^/]+)$/.exec(path);
  if (m && UUID.test(m[1])) return { name: 'detail', episodeId: m[1].toLowerCase() };
  return { name: 'notfound', path };
}

export function detailHref(episodeId) {
  return `#/episodes/${episodeId}`;
}
