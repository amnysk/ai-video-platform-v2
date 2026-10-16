"""本文取得（``ContentFetcher`` の実装、ADR-0036）。httpx を使ってよいのはこのファイルだけ。

**この Port の役割は「取得」であって「解釈」ではない。**
外部ページの本文は**データ**で、命令ではない。抽出したテキストはそのまま返し、
本文中の指示・リンクには従わない（本文中の URL を取得しに行かない）。

守るもの（すべて ``tests/unit/test_http_content_fetcher.py`` が固定する）:

- 取得の入口は ``fetch`` だけ。各 hop（最初の URL と、リダイレクトのたびの URL）で
  ``UrlGuard.check`` を通る。guard を通らずに接続する経路は無い（``_send_pinned`` は
  ``GuardedTarget`` を要求し、``tests/architecture/test_research_no_live_network.py``
  が呼び出しを縛る）
- **接続は検査済みの IP へ固定する**（DNS rebinding 対策）。URL の host を IP に置き換え、
  ``Host`` ヘッダと TLS の SNI / 証明書検証には元の名前を使う（``sni_hostname``）。
  解決は hop ごとに 1 回で、その結果だけを使う
- リダイレクトは自前で追う（最大 5、``follow_redirects=False``）。``https`` → ``http`` の降格は拒否
- 環境のプロキシ・netrc を使わない（``trust_env=False``）。Cookie を保存も送信もしない。
  ``Authorization`` を送らない。User-Agent は固定
- connect / read の timeout に加え、**全体の期限**（遅いストリームで居座られない）
- 本文サイズの上限（既定 2 MiB）を ``Content-Length`` と**ストリーム**の両方で守る。
  圧縮は ``gzip`` / ``deflate`` だけを名乗り、**自前で上限付きに展開する**
  （展開後のサイズも上限。bomb 対策）

**結果の意味**（期待される失敗は例外ではなく値）:

- ``fetched``: 本文テキストを完全に読めた。``body_confirmed`` が真になる唯一の状態
- ``truncated``: ストリームが上限を超えたので先頭だけを保持した（``error="too_large"``）。
  確認済みではない。
  宣言された ``Content-Length`` が上限を超えるときは読まずに ``failed``（``too_large``）にする
- ``failed``: 禁止 URL・timeout・4xx/5xx・許可外の型など。``FetchedContent.error`` が種別

**content-type の allowlist**（``ALLOWED_CONTENT_TYPES``）: ``text/html`` ``application/xhtml+xml``
（タグを除いたテキスト）、``text/plain`` ``application/json``（そのまま）、``application/pdf``。
**PDF は取得するがテキスト化しない**（追加の依存を持たない。ADR-0009）。テキストの無い資料を
「本文確認済み」にしないため、``fetched`` にはせず ``failed`` / ``bad_content_type`` /
``error_detail="no_text_extractor"`` にする。取得した事実として ``content_sha256`` と
``bytes_read`` は残す。
許可外（画像・動画・不明・型なし）は本文を**読まずに**拒否する。

文字コードは宣言（``Content-Type`` の charset → HTML の ``<meta charset>``）を使い、
読めない・未知・テキストでない名前は UTF-8 に落とし、不正なバイトは ``errors="replace"``。
"""

from __future__ import annotations

import asyncio
import codecs
import hashlib
import http.cookiejar
import re
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin, urlsplit

import httpx

from domain.research.ports import FetchedContent, FetchErrorKind, FetchStatus
from infrastructure.research.errors import ResolutionError, UrlNotAllowed
from infrastructure.research.url_guard import GuardedTarget, Resolver, UrlGuard, redact_url

USER_AGENT = "avp-research-fetcher/1.0 (read-only; no cookies)"
DEFAULT_MAX_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_REDIRECTS = 5
DEFAULT_CONNECT_TIMEOUT = 5.0
DEFAULT_READ_TIMEOUT = 10.0
DEFAULT_TOTAL_TIMEOUT = 30.0
#: 1 つの名前が複数のアドレスを返すとき、接続失敗で順に試す最大数
MAX_ADDRESS_ATTEMPTS = 4

HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})
PLAIN_TYPES = frozenset({"text/plain", "application/json"})
PDF_TYPES = frozenset({"application/pdf"})
ALLOWED_CONTENT_TYPES = HTML_TYPES | PLAIN_TYPES | PDF_TYPES

REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
#: 自前で展開できる Content-Encoding。これ以外は名乗らない・受け取らない
ACCEPT_ENCODING = "gzip, deflate"
_ACCEPT = "text/html, application/xhtml+xml, text/plain, application/json, application/pdf;q=0.5"

_META_CHARSET = re.compile(rb"<meta[^>]+charset\s*=\s*[\"']?\s*([A-Za-z0-9_.:\-]+)", re.IGNORECASE)


# --- HTML → テキスト -------------------------------------------------------------------

_SKIPPED_TAGS = frozenset({"script", "style", "noscript", "template", "svg", "iframe", "object"})
_BLOCK_TAGS = frozenset(
    {
        "address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt",
        "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6",
        "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section", "table",
        "tbody", "td", "tfoot", "th", "thead", "title", "tr", "ul",
    }
)  # fmt: skip
_VOID_TAGS = frozenset({"br", "hr", "img", "input", "link", "meta", "area", "base", "col", "embed"})
_SPACES = re.compile(r"[ \t\r\f\v 　]+")


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIPPED_TAGS:
            self._skip += 1
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIPPED_TAGS and self._skip:
            self._skip -= 1
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self._parts.append(data)

    def text(self) -> str:
        lines = (_SPACES.sub(" ", line).strip() for line in "".join(self._parts).split("\n"))
        return "\n".join(line for line in lines if line)


def extract_text(html: str) -> str:
    """タグ・script・style を除いた本文テキスト。中身の解釈も実行もしない（データのまま）。"""
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    return parser.text()


# --- 圧縮 ---------------------------------------------------------------------------------


class _BadBody(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class _Inflater:
    """上限付きの gzip / deflate 展開。``feed`` は ``max_length`` を超える出力を作らない。"""

    def __init__(self, encoding: str) -> None:
        self._gzip = encoding in ("gzip", "x-gzip")
        self._first = True
        self._decoder = zlib.decompressobj(zlib.MAX_WBITS | 16 if self._gzip else zlib.MAX_WBITS)

    @property
    def finished(self) -> bool:
        return self._decoder.eof

    def feed(self, data: bytes, max_length: int) -> bytes:
        try:
            return self._decode(data, max_length)
        except zlib.error:
            if self._first and not self._gzip:  # ``deflate`` には zlib ヘッダ無しの生データも多い
                self._decoder = zlib.decompressobj(-zlib.MAX_WBITS)
                try:
                    return self._decode(data, max_length)
                except zlib.error:
                    pass
            raise _BadBody("bad_compressed_body") from None
        finally:
            self._first = False

    def _decode(self, data: bytes, max_length: int) -> bytes:
        return self._decoder.decompress(data, max_length)


def _decode_text(data: bytes, content_type: str, is_html: bool) -> str:
    charset = _declared_charset(content_type)
    if charset is None and is_html:
        match = _META_CHARSET.search(data[:4096])
        charset = match.group(1).decode("ascii", "replace") if match else None
    if data.startswith(codecs.BOM_UTF8):
        charset = "utf-8-sig"
    for candidate in (charset, "utf-8"):
        if candidate is None:
            continue
        try:
            return data.decode(candidate, errors="replace")
        except LookupError:  # 未知の名前・zlib / base64 などテキストでない codec
            continue
    return data.decode("utf-8", errors="replace")  # pragma: no cover - utf-8 は必ずある


def _declared_charset(content_type: str) -> str | None:
    for param in content_type.split(";")[1:]:
        name, _, value = param.partition("=")
        if name.strip().lower() == "charset":
            return value.strip().strip("\"'") or None
    return None


# --- 取得 --------------------------------------------------------------------------------------


_LOCATION = "x-avp-redirect-location"


class _LocationShield(httpx.AsyncBaseTransport):
    """``Location`` を httpx に解釈させない。

    httpx は ``follow_redirects=False`` でも ``Location`` から次のリクエストを先に組み立て、
    不正な値で例外を投げる。リダイレクトは自前（guard を通して）で追うので、
    ``Location`` を別名の header に移して httpx の解釈を止める。
    """

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._inner.handle_async_request(request)
        location = response.headers.get("location")
        if response.status_code in REDIRECT_STATUSES and location is not None:
            del response.headers["location"]
            response.headers[_LOCATION] = location
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()


@dataclass(slots=True)
class _Trace:
    """1 回の ``fetch`` で起きたこと。失敗のときも結果へ写す。"""

    requested: str
    final: str
    chain: list[str] = field(default_factory=list)
    status: int | None = None
    content_type: str | None = None


@dataclass(frozen=True, slots=True)
class _Body:
    data: bytes
    truncated: bool


class HttpContentFetcher:
    """``ContentFetcher`` の httpx 実装。公開する取得メソッドは ``fetch`` だけ。"""

    def __init__(
        self,
        resolver: Resolver | None = None,
        *,
        guard: UrlGuard | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_redirects: int = DEFAULT_MAX_REDIRECTS,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        read_timeout: float = DEFAULT_READ_TIMEOUT,
        total_timeout: float = DEFAULT_TOTAL_TIMEOUT,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if guard is not None and resolver is not None:
            raise ValueError("pass either a resolver or a guard, not both")
        if max_bytes <= 0 or max_redirects < 0 or total_timeout <= 0:
            raise ValueError("limits must be positive")
        self._guard = guard or UrlGuard(resolver)
        self._transport = transport
        self._max_bytes = max_bytes
        self._max_redirects = max_redirects
        self._timeout = httpx.Timeout(
            connect=connect_timeout, read=read_timeout, write=read_timeout, pool=connect_timeout
        )
        self._total_timeout = total_timeout
        self._clock = clock or (lambda: datetime.now(UTC))

    async def fetch(self, url: str) -> FetchedContent:
        """``url`` を取得する。期待される失敗は例外にせず ``FetchedContent`` の値で返す。"""
        trace = _Trace(requested=redact_url(url), final=redact_url(url))
        try:
            async with asyncio.timeout(self._total_timeout):
                # 取得ごとに新しい client（Cookie・接続を共有しない）
                async with self._client() as client:
                    return await self._follow(client, url, trace)
        except TimeoutError:
            return self._failed(trace, "timeout", "total_timeout")
        except httpx.TimeoutException:
            return self._failed(trace, "timeout", "transport_timeout")
        except httpx.TransportError as exc:
            return self._failed(trace, "network", type(exc).__name__)

    def _client(self) -> httpx.AsyncClient:
        inner = self._transport or httpx.AsyncHTTPTransport(
            limits=httpx.Limits(max_keepalive_connections=0)
        )
        client = httpx.AsyncClient(
            transport=_LocationShield(inner),
            follow_redirects=False,
            trust_env=False,
            timeout=self._timeout,
        )
        # 全ての Cookie を拒否する policy。Set-Cookie を受けても保存しない（送信もしない）
        client.cookies.jar.set_policy(http.cookiejar.DefaultCookiePolicy(allowed_domains=[]))
        return client

    async def _follow(self, client: httpx.AsyncClient, url: str, trace: _Trace) -> FetchedContent:
        current = url
        for hop in range(self._max_redirects + 1):
            try:
                target = await self._guard.check(current)
            except UrlNotAllowed as exc:
                return self._failed(trace, "blocked_url", exc.reason)
            except ResolutionError:
                return self._failed(trace, "network", "dns")
            trace.final = target.url
            response = await self._send_pinned(client, target)
            try:
                trace.status = response.status_code
                trace.content_type = response.headers.get("content-type")
                if response.status_code not in REDIRECT_STATUSES:
                    return await self._finish(response, trace)
                trace.chain.append(target.url)
                location = response.headers.get(_LOCATION)
            finally:
                await response.aclose()
            if not location:
                return self._failed(trace, "blocked_url", "redirect_without_location")
            if hop == self._max_redirects:
                return self._failed(trace, "blocked_url", "too_many_redirects")
            current = urljoin(target.url, location)
            if target.scheme == "https" and urlsplit(current).scheme.lower() == "http":
                return self._failed(trace, "blocked_url", "scheme_downgrade")
        return self._failed(trace, "blocked_url", "too_many_redirects")  # pragma: no cover

    async def _send_pinned(
        self, client: httpx.AsyncClient, target: GuardedTarget
    ) -> httpx.Response:
        """検査済みの IP へだけ接続する。名前は ``Host`` と SNI（証明書検証）に使う。"""
        last: httpx.TransportError | None = None
        for address in target.addresses[:MAX_ADDRESS_ATTEMPTS]:
            request = client.build_request(
                "GET",
                httpx.URL(target.url).copy_with(host=address),
                headers={
                    "Host": target.host_header,
                    "User-Agent": USER_AGENT,
                    "Accept": _ACCEPT,
                    "Accept-Encoding": ACCEPT_ENCODING,
                    "Connection": "close",
                },
                extensions={"sni_hostname": target.host} if target.scheme == "https" else None,
            )
            try:
                return await client.send(request, stream=True)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                last = exc  # 次の検査済みアドレスを試す
        assert last is not None
        raise last

    # --- 応答の扱い --------------------------------------------------------------------------

    async def _finish(self, response: httpx.Response, trace: _Trace) -> FetchedContent:
        status = response.status_code
        if 400 <= status < 500:
            return self._failed(trace, "http_4xx", f"status_{status}")
        if 500 <= status < 600:
            return self._failed(trace, "http_5xx", f"status_{status}")
        if not 200 <= status < 300:
            return self._failed(trace, "network", f"unexpected_status_{status}")

        content_type = (trace.content_type or "").split(";")[0].strip().lower()
        if content_type not in ALLOWED_CONTENT_TYPES:
            return self._failed(trace, "bad_content_type", "content_type_not_allowed")
        declared = _declared_length(response)
        if declared is not None and declared > self._max_bytes:
            return self._failed(trace, "too_large", "content_length")
        encodings = [
            e.strip().lower() for e in response.headers.get("content-encoding", "").split(",")
        ]
        encodings = [e for e in encodings if e and e != "identity"]
        if len(encodings) > 1 or (encodings and encodings[0] not in ("gzip", "x-gzip", "deflate")):
            return self._failed(trace, "bad_content_type", "unsupported_content_encoding")

        try:
            body = await self._read(response, _Inflater(encodings[0]) if encodings else None)
        except _BadBody as exc:
            return self._failed(trace, "network", exc.detail)
        sha256 = hashlib.sha256(body.data).hexdigest()
        if content_type in PDF_TYPES:  # 取得はした。テキスト化はしない（モジュール docstring）
            return self._failed(
                trace,
                "bad_content_type",
                "no_text_extractor",
                bytes_read=len(body.data),
                sha256=sha256,
            )
        text = _decode_text(body.data, trace.content_type or "", content_type in HTML_TYPES)
        if content_type in HTML_TYPES:
            text = extract_text(text)
        return FetchedContent(
            requested_url=trace.requested,
            final_url=urldefrag(trace.final).url,
            redirect_chain=tuple(trace.chain),
            status_code=status,
            content_type=trace.content_type,
            text=text,
            content_sha256=sha256,
            bytes_read=len(body.data),
            truncated=body.truncated,
            fetched_at=self._clock(),
            fetch_status="truncated" if body.truncated else "fetched",
            error="too_large" if body.truncated else None,
            error_detail="stream_exceeded" if body.truncated else None,
        )

    async def _read(self, response: httpx.Response, inflater: _Inflater | None) -> _Body:
        """上限まで読む。圧縮は展開後のサイズで数える（``aiter_raw`` + 上限付き展開）。"""
        out = bytearray()
        wire = 0
        truncated = False
        async for chunk in response.aiter_raw():
            wire += len(chunk)
            if inflater is not None:
                if wire > self._max_bytes:  # 圧縮のままでも上限を超えるなら打ち切る
                    truncated = True
                    break
                piece = inflater.feed(chunk, self._max_bytes - len(out) + 1)
            else:
                piece = chunk
            room = self._max_bytes - len(out)
            if len(piece) > room:
                out += piece[:room]
                truncated = True
                break
            out += piece
        if inflater is not None and not truncated and wire and not inflater.finished:
            raise _BadBody("incomplete_compressed_body")
        return _Body(bytes(out), truncated)

    def _failed(
        self,
        trace: _Trace,
        kind: FetchErrorKind,
        detail: str,
        *,
        bytes_read: int = 0,
        sha256: str | None = None,
    ) -> FetchedContent:
        status: FetchStatus = "failed"
        return FetchedContent(
            requested_url=trace.requested,
            final_url=urldefrag(trace.final).url,
            redirect_chain=tuple(trace.chain),
            status_code=trace.status,
            content_type=trace.content_type,
            text=None,
            content_sha256=sha256,
            bytes_read=bytes_read,
            truncated=False,
            fetched_at=self._clock(),
            fetch_status=status,
            error=kind,
            error_detail=detail,
        )


def _declared_length(response: httpx.Response) -> int | None:
    value = response.headers.get("content-length")
    if value is None or not value.strip().isdigit():
        return None
    return int(value)
