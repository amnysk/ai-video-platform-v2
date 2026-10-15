// Python 側の enum 宣言を読む補助（定義を JS に二重に書かず、突き合わせて検査するため）。
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
export const repoPath = (...p) => path.join(ROOT, ...p);

/** `class Name(StrEnum):` の `MEMBER = "value"` の value 一覧 */
export function pyEnumValues(file, className) {
  const src = readFileSync(repoPath(file), 'utf8');
  const start = src.indexOf(`class ${className}(`);
  if (start < 0) throw new Error(`${className} not found in ${file}`);
  const rest = src.slice(start).split('\n').slice(1);
  const values = [];
  for (const line of rest) {
    if (/^\S/.test(line) && line.trim() !== '') break; // 次のトップレベル
    const m = /^\s{4}[A-Z_]+ = "([^"]+)"/.exec(line);
    if (m) values.push(m[1]);
  }
  return values;
}
