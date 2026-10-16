// 表示の整形。DOM に触れない純関数だけ（node --test で検査する）。
// 原則（ADR-0045 §6）: 記録が無い項目は「未記録」。推測・補完しない。

export const MISSING = '未記録';
export const LLM_COST_UNMEASURED_NOTE =
  'unmeasured（LLM の費用は計測されていません。0 円ではありません）';
export const COST_NOT_ESTIMATED = '未見積り（0 円ではありません）';

export function isBlank(value) {
  return value === null || value === undefined || value === '';
}

/** null / undefined / 空文字だけを「未記録」にする。0 や false は記録された値。 */
export function orMissing(value) {
  return isBlank(value) ? MISSING : String(value);
}

export function formatDateTime(value) {
  if (isBlank(value)) return MISSING;
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return String(value);
  return d.toLocaleString('ja-JP', { timeZone: 'Asia/Tokyo', hour12: false }) + ' JST';
}

/** 時刻だけ（JST）。「最終取得」のように、いつの情報かを示すために使う。 */
export function formatTime(value) {
  if (isBlank(value)) return MISSING;
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return String(value);
  return d.toLocaleTimeString('ja-JP', { timeZone: 'Asia/Tokyo', hour12: false }) + ' JST';
}

/** USD の10進文字列。null は「未見積り」（0 として表示しない）。 */
export function formatUsd(value) {
  if (isBlank(value)) return COST_NOT_ESTIMATED;
  return `$${value}`;
}

export function shortId(value) {
  if (isBlank(value)) return MISSING;
  const s = String(value);
  return s.length > 12 ? `${s.slice(0, 8)}…` : s;
}

/** 配列が空なら null（呼び出し側が「未記録」を出す）。 */
export function nonEmpty(list) {
  return Array.isArray(list) && list.length > 0 ? list : null;
}

/** 任意の値を1行の安全な文字列にする（payload 全文を出さないための要約用。深さ・長さ制限付き）。 */
export function briefValue(value, maxLen = 120) {
  if (isBlank(value)) return MISSING;
  if (typeof value === 'string') return value.length > maxLen ? `${value.slice(0, maxLen)}…` : value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  return Array.isArray(value) ? `[${value.length} 件]` : '{…}';
}
