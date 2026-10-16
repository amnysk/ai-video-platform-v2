// DOM 生成の最小ヘルパ。**サーバー由来の文字列は必ず textContent 経由**（innerHTML は使わない）。
// 属性は setAttribute、イベントは addEventListener（CSP: inline script / handler / style 属性を使わない）。

const FORBIDDEN_ATTRS = new Set(['style', 'innerhtml', 'outerhtml', 'srcdoc']);

export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs ?? {})) {
    if (v === null || v === undefined || v === false) continue;
    const key = k.toLowerCase();
    if (FORBIDDEN_ATTRS.has(key)) continue;
    if (key.startsWith('on')) {
      if (typeof v === 'function') el.addEventListener(key.slice(2), v);
      continue; // 文字列の on* 属性は付けない
    }
    if (k === 'class') el.className = v;
    else if (k === 'text') el.textContent = String(v);
    else el.setAttribute(k, v === true ? '' : String(v));
  }
  append(el, children);
  return el;
}

export function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

export function clear(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
  return el;
}

export function replaceChildren(el, ...children) {
  clear(el);
  return append(el, children);
}
