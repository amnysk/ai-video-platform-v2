import { h, replaceChildren } from '../dom.js';
import { badge, errorBox, loading } from '../components.js';
import { buildListQuery, mergePage, nextCursor } from '../cursor.js';
import { EPISODE_STATUS_LABEL, episodeStatusLabel, needsAttention, pipelineKindLabel } from '../status.js';
import { formatDateTime, orMissing } from '../format.js';
import { detailHref } from '../router.js';

const PAGE_SIZE = 50;

export function listView(ctx) {
  const spec = ctx.ops.byName.get('list_episodes');
  let items = [];
  let cursor = null;
  let seq = 0; // 古い応答を捨てる
  let disposed = false;

  const q = h('input', { type: 'search', id: 'q', name: 'q', autocomplete: 'off' });
  const status = h(
    'select',
    { id: 'status', name: 'status' },
    h('option', { value: '' }, 'すべて'),
    Object.entries(EPISODE_STATUS_LABEL).map(([v, l]) => h('option', { value: v }, `${l}（${v}）`)),
  );
  const form = h(
    'form',
    { class: 'toolbar', role: 'search', onsubmit: (e) => { e.preventDefault(); load(true); } },
    h('div', {}, h('label', { for: 'q' }, 'トピック検索'), q),
    h('div', {}, h('label', { for: 'status' }, '状態で絞り込み'), status),
    h('div', {}, h('button', { type: 'submit', class: 'btn btn-primary' }, '検索')),
  );
  const body = h('div', {});
  const more = h('div', { class: 'actions' });

  async function load(reset) {
    const my = ++seq;
    if (reset) {
      items = [];
      cursor = null;
    }
    replaceChildren(more);
    if (reset) replaceChildren(body, loading());
    try {
      const { data } = await ctx.api.get(spec.path, {
        query: buildListQuery({ limit: PAGE_SIZE, before: cursor?.before, beforeId: cursor?.beforeId, status: status.value, q: q.value }),
      });
      if (disposed || my !== seq) return;
      const merged = mergePage(items, data);
      items = merged.items;
      cursor = nextCursor(data);
      render(merged.added, data.episodes?.length ?? 0);
    } catch (err) {
      if (disposed || my !== seq || err?.name === 'AbortError') return;
      replaceChildren(body, errorBox(err, { onRetry: () => load(reset) }));
    }
  }

  function render(added, got) {
    if (items.length === 0) {
      replaceChildren(body, h('p', {}, '該当する Episode はありません。'));
      replaceChildren(more);
      return;
    }
    const table = h(
      'table',
      {},
      h('caption', {}, `${items.length} 件を表示中（新しい順）`),
      h('thead', {}, h('tr', {}, ['トピック', '状態', '種別', '作成', '更新'].map((t) => h('th', { scope: 'col' }, t)))),
      h(
        'tbody',
        {},
        items.map((e) => {
          const attn = needsAttention(e.status);
          return h(
            'tr',
            {},
            h('td', {}, h('a', { href: detailHref(e.episode_id) }, e.topic ? e.topic : '（トピック未記録）'), h('div', { class: 'episode-id muted' }, e.episode_id)),
            h(
              'td',
              {},
              h('div', {}, episodeStatusLabel(e.status), ' ', attn ? badge(`要注意: ${attn}`, 'attention') : null),
              h('div', { class: 'muted small' }, orMissing(e.status)),
            ),
            h('td', {}, pipelineKindLabel(e.pipeline_kind)),
            h('td', {}, formatDateTime(e.created_at)),
            h('td', {}, formatDateTime(e.updated_at)),
          );
        }),
      ),
    );
    replaceChildren(body, h('div', { class: 'table-wrap' }, table));
    const parts = [];
    if (cursor) {
      parts.push(h('button', { type: 'button', class: 'btn', onclick: () => load(false) }, 'さらに読み込む'));
      if (cursor.degraded) {
        parts.push(h('p', { class: 'muted small' }, 'サーバーが複合 cursor（next_before_id）を返していません。同じ時刻の行が前後する場合は ID で重複を除きますが、取りこぼす可能性があります。'));
      }
    } else {
      parts.push(h('p', { class: 'muted' }, 'これ以上はありません。'));
    }
    if (added === 0 && got > 0) parts.push(h('p', { class: 'muted small' }, '新しい行はありませんでした（重複のみ）。'));
    replaceChildren(more, ...parts);
    ctx.announce(`${items.length} 件を表示しました`);
  }

  const el = h('section', {}, h('h2', {}, 'Episode 一覧'), form, body, more);
  load(true);
  return {
    el,
    dispose() {
      disposed = true;
    },
  };
}
