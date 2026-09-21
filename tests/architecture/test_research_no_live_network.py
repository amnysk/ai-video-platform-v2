"""research の外部通信の入口を数えられる場所に閉じ込める（INV-6 / INV-18、ADR-0031 §7）。

- ``domain/research/`` は純粋（HTTP・socket・TLS を import しない）
- ``infrastructure/research/`` で httpx を import してよいのは ``http_fetcher.py`` だけ。
  socket は名前解決の ``url_guard.py`` だけ。Fake は通信を持たない
- ``HttpContentFetcher`` の取得は必ず ``UrlGuard.check`` を通る（guard を通らない経路が無い）
- YouTube / Google のエンドポイント文字列を research のコードに書かない
  （書けるのは ``infrastructure/youtube/*.py``。``test_no_live_calls.py`` の規則と同じ集合）
"""

from __future__ import annotations

import ast
import pathlib

from tests.architecture.test_no_live_calls import FAL_TOKENS, YOUTUBE_TOKENS

REPO = pathlib.Path(__file__).resolve().parents[2]
DOMAIN_RESEARCH = REPO / "domain" / "research"
INFRA_RESEARCH = REPO / "infrastructure" / "research"
FETCHER = INFRA_RESEARCH / "http_fetcher.py"
URL_GUARD = INFRA_RESEARCH / "url_guard.py"

NETWORK_MODULES = ("httpx", "requests", "aiohttp", "urllib3", "urllib.request", "http.client")
LOW_LEVEL_MODULES = ("socket", "ssl")


def _files(directory: pathlib.Path) -> list[pathlib.Path]:
    return sorted(directory.rglob("*.py"))


def _imports(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def _imports_any(path: pathlib.Path, modules: tuple[str, ...]) -> list[str]:
    return sorted(
        name
        for name in _imports(path)
        if any(name == m or name.startswith(m + ".") for m in modules)
    )


def test_the_research_packages_exist() -> None:
    assert (DOMAIN_RESEARCH / "ports.py").is_file()
    assert FETCHER.is_file() and URL_GUARD.is_file()


def test_domain_research_is_pure() -> None:
    forbidden = (*NETWORK_MODULES, *LOW_LEVEL_MODULES, "infrastructure", "apps", "workers")
    violations = [
        f"{p.relative_to(REPO)} imports {name}"
        for p in _files(DOMAIN_RESEARCH)
        for name in _imports_any(p, forbidden)
    ]
    assert not violations, "\n".join(violations)


def test_only_the_http_fetcher_imports_an_http_client() -> None:
    violations = [
        f"{p.relative_to(REPO)} imports {name}"
        for p in _files(INFRA_RESEARCH)
        if p != FETCHER
        for name in _imports_any(p, NETWORK_MODULES)
    ]
    assert not violations, "\n".join(violations)
    assert _imports_any(FETCHER, ("httpx",))


def test_only_the_guard_touches_sockets_and_nobody_touches_tls() -> None:
    violations = [
        f"{p.relative_to(REPO)} imports {name}"
        for p in _files(INFRA_RESEARCH)
        for name in _imports_any(p, ("ssl",))
    ] + [
        f"{p.relative_to(REPO)} imports {name}"
        for p in _files(INFRA_RESEARCH)
        if p != URL_GUARD
        for name in _imports_any(p, ("socket",))
    ]
    assert not violations, "\n".join(violations)


def test_the_fakes_have_no_way_to_reach_the_network() -> None:
    for name in ("fake_providers.py", "fake_corpus.py", "not_configured.py"):
        path = INFRA_RESEARCH / name
        assert not _imports_any(path, (*NETWORK_MODULES, *LOW_LEVEL_MODULES)), name
        assert not _imports_any(path, ("infrastructure.research.http_fetcher",)), name
        assert not _imports_any(path, ("infrastructure.research.url_guard",)), name
        assert not _imports_any(path, ("infrastructure.youtube",)), name


def test_research_code_has_no_provider_endpoint_strings() -> None:
    tokens = {*YOUTUBE_TOKENS, *FAL_TOKENS, "googleapis.com", "api.openai.com", "api.anthropic.com"}
    violations: list[str] = []
    for directory in (DOMAIN_RESEARCH, INFRA_RESEARCH):
        for path in _files(directory):
            source = path.read_text(encoding="utf-8")
            violations += [
                f"{path.relative_to(REPO)} mentions {token} (INV-18)"
                for token in sorted(tokens)
                if token in source
            ]
    assert not violations, "\n".join(violations)


# --- HttpContentFetcher は必ず guard を通る ---------------------------------------------------

_HTTP_CALLS = frozenset(
    {"send", "request", "stream", "get", "post", "put", "patch", "delete", "head", "options"}
)


def _fetcher_tree() -> ast.Module:
    return ast.parse(FETCHER.read_text(encoding="utf-8"), filename=str(FETCHER))


def _functions(tree: ast.AST) -> list[ast.AsyncFunctionDef | ast.FunctionDef]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef | ast.FunctionDef)]


def _attr_calls(node: ast.AST) -> set[str]:
    return {
        n.func.attr
        for n in ast.walk(node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }


def _client_calls(node: ast.AST) -> set[str]:
    """``client`` に対する送信系の呼び出し（``headers.get`` などは含めない）。"""
    return {
        n.func.attr
        for n in ast.walk(node)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr in _HTTP_CALLS
        and "client" in ast.unparse(n.func.value).lower()
    }


def _class(tree: ast.Module, name: str) -> ast.ClassDef:
    return next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name)


def test_the_fetcher_has_one_public_fetch_method() -> None:
    cls = _class(_fetcher_tree(), "HttpContentFetcher")
    public = {
        n.name
        for n in cls.body
        if isinstance(n, ast.AsyncFunctionDef | ast.FunctionDef) and not n.name.startswith("_")
    }
    assert public == {"fetch"}


def test_every_http_call_is_in_send_pinned_which_requires_a_guarded_target() -> None:
    tree = _fetcher_tree()
    callers = {fn.name for fn in _functions(tree) if _client_calls(fn)}
    # ``_send_pinned`` 以外で httpx の送信を呼ばない（``client.send`` は 1 か所）
    assert callers == {"_send_pinned"}, callers
    send_pinned = next(fn for fn in _functions(tree) if fn.name == "_send_pinned")
    annotations = [ast.unparse(a.annotation) for a in send_pinned.args.args if a.annotation]
    assert "GuardedTarget" in annotations  # guard の結果でなければ渡せない形


def test_every_caller_of_send_pinned_checks_the_url_first() -> None:
    tree = _fetcher_tree()
    callers = [
        fn
        for fn in _functions(tree)
        if any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "_send_pinned"
            for n in ast.walk(fn)
        )
    ]
    assert callers, "nothing calls _send_pinned"
    for fn in callers:
        checks = [
            n
            for n in ast.walk(fn)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "check"
            and ast.unparse(n.func.value) == "self._guard"
        ]
        assert checks, f"{fn.name} sends without UrlGuard.check"
        sends = [
            n
            for n in ast.walk(fn)
            if isinstance(n, ast.Call) and "_send_pinned" in ast.unparse(n.func)
        ]
        assert min(c.lineno for c in checks) < min(s.lineno for s in sends)


def test_fetch_reaches_the_guarded_loop_and_the_loop_runs_per_hop() -> None:
    tree = _fetcher_tree()
    fetch = next(fn for fn in _functions(tree) if fn.name == "fetch")
    assert "_follow" in _attr_calls(fetch)
    follow = next(fn for fn in _functions(tree) if fn.name == "_follow")
    loops = [n for n in ast.walk(follow) if isinstance(n, ast.For | ast.While)]
    assert loops, "redirects must be followed in a loop that re-checks each hop"
    loop = loops[0]
    assert "check" in _attr_calls(loop) and "_send_pinned" in _attr_calls(loop)


def test_the_fetcher_never_lets_httpx_follow_redirects_or_use_the_environment() -> None:
    source = FETCHER.read_text(encoding="utf-8")
    assert "follow_redirects=False" in source
    assert "trust_env=False" in source
    assert "follow_redirects=True" not in source
