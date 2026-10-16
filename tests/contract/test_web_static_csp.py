"""運用者 UI の静的ファイルが CSP（``default-src 'self'``）を破らないことの検査（ADR-0045 §1）。

inline script / inline style / on* 属性 / style 属性 / 外部 URL を持ち込まない。JS・CSS も対象:
HTML だけ綺麗でも、モジュールが innerHTML や外部 fetch を足せば同じ事故になる。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[2] / "apps" / "web" / "static"
INDEX = STATIC / "index.html"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_index_exists() -> None:
    assert INDEX.is_file()


def test_index_has_no_inline_script_or_style() -> None:
    html = _text(INDEX)
    for m in re.finditer(r"<script\b([^>]*)>", html, re.IGNORECASE):
        assert re.search(r"\bsrc\s*=", m.group(1)), f"inline <script>: {m.group(0)}"
    assert not re.search(r"<style\b", html, re.IGNORECASE), "inline <style>"


def test_index_has_no_event_handler_or_style_attributes() -> None:
    html = _text(INDEX)
    assert not re.search(r"\son[a-z]+\s*=", html, re.IGNORECASE), "on*= attribute"
    assert not re.search(r"\sstyle\s*=", html, re.IGNORECASE), "style= attribute"
    assert "javascript:" not in html.lower()


def test_index_has_no_external_urls() -> None:
    assert not re.search(r"https?://", _text(INDEX), re.IGNORECASE)
    assert not re.search(r"""(?:src|href)\s*=\s*["']//""", _text(INDEX))


def _sources() -> list[Path]:
    return sorted([*STATIC.rglob("*.js"), *STATIC.rglob("*.css")])


def test_sources_exist() -> None:
    assert any(p.suffix == ".js" for p in _sources())


@pytest.mark.parametrize("path", _sources(), ids=lambda p: str(p.relative_to(STATIC)))
def test_js_and_css_do_not_bypass_csp(path: Path) -> None:
    src = _text(path)
    code = re.sub(r"//[^\n]*|/\*.*?\*/", "", src, flags=re.DOTALL)  # コメント内の言及は許す
    assert not re.search(r"https?://", code), "external URL"
    if path.suffix == ".css":
        assert "@import" not in code
        return
    assert not re.search(r"\.(?:inner|outer)HTML\b", code), "innerHTML/outerHTML は使わない"
    assert "insertAdjacentHTML" not in code
    assert "document.write" not in code
    assert not re.search(r"\beval\s*\(|new\s+Function\s*\(", code)
    assert "localStorage" not in code, "トークンを localStorage に置かない（sessionStorage のみ）"
    assert not re.search(r"""setAttribute\(\s*['"](?:on|style)""", code, re.IGNORECASE)
    assert not re.search(r"console\.\w+\(", code), "console 出力でトークン等を漏らさない"
