"""SSRF 規則（ADR-0031 §7）。外部ページ・検索結果にあった URL を取得する前に必ず通す。

守るもの: 内部ネットワーク・クラウドのメタデータ・ローカルのファイルへ到達しないこと。

- scheme は ``http`` / ``https`` だけ。userinfo（``user:pass@``）付きは拒否
- **接続先の IP を検査する**。名前は解決して、返った**全ての**アドレスが公開アドレスのときだけ通す
  （公開と private の混在も拒否。どれに繋がるかを攻撃者に選ばせない）
- IP は書き方に依らず同じ規則: 10 進 ``2130706433`` / 16 進 ``0x7f.1`` / 8 進 ``0177.0.0.1`` /
  省略形 ``127.1``（getaddrinfo・inet_aton が受け入れる書き方）、IPv4-mapped / 6to4 / NAT64 の
  IPv6、全角数字・Unicode のドット（IDNA の正規化で数字になる）
- loopback / private / link-local / multicast / reserved / unspecified / carrier-grade NAT
  （100.64.0.0/10）/ metadata（``169.254.169.254``、``fd00:ec2::254``）と、名前
  （``localhost`` ``*.localhost`` ``*.local`` ``*.internal`` ``metadata.google.internal``、
  単一ラベルの名前）
- 検査した IP を返す（``GuardedTarget.addresses``）。**接続はその IP へ固定する**
  （DNS rebinding 対策。``http_fetcher.py`` が IP へ接続し、``Host`` / SNI に名前を使う）。
  この後で再解決しない

拒否は ``UrlNotAllowed``（同じ入力で必ず同じ結果 = permanent）。名前解決の失敗は ``ResolutionError``
（通信障害 = retryable）で、方針違反とは別。例外に URL・userinfo を入れない。
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

from infrastructure.research.errors import ResolutionError, UrlNotAllowed

MAX_URL_LENGTH = 2048
ALLOWED_SCHEMES = frozenset({"http", "https"})
DEFAULT_PORTS = {"http": 80, "https": 443}

#: 名前そのものが内部向け。ここに無くても、解決先の IP で必ず再検査する
BLOCKED_NAMES = frozenset(
    {
        "localhost",
        "metadata",
        "metadata.google.internal",
        "metadata.goog",
        "instance-data",
    }
)
BLOCKED_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".localdomain",
    ".home.arpa",
)

_BLOCKED_NETWORKS: tuple[tuple[str, str], ...] = (
    ("0.0.0.0/8", "unspecified"),
    ("10.0.0.0/8", "private"),
    ("100.64.0.0/10", "carrier_grade_nat"),
    ("127.0.0.0/8", "loopback"),
    ("169.254.169.254/32", "metadata"),
    ("169.254.0.0/16", "link_local"),
    ("172.16.0.0/12", "private"),
    ("192.0.0.0/24", "ietf_protocol"),
    ("192.0.2.0/24", "documentation"),
    ("192.88.99.0/24", "6to4_relay"),
    ("192.168.0.0/16", "private"),
    ("198.18.0.0/15", "benchmarking"),
    ("198.51.100.0/24", "documentation"),
    ("203.0.113.0/24", "documentation"),
    ("224.0.0.0/4", "multicast"),
    ("240.0.0.0/4", "reserved"),
    ("::/128", "unspecified"),
    ("::1/128", "loopback"),
    ("::/8", "reserved"),
    ("64:ff9b::/96", "nat64"),
    ("64:ff9b:1::/48", "nat64"),
    ("100::/64", "discard"),
    ("2001::/23", "ietf_protocol"),
    ("2001:db8::/32", "documentation"),
    ("2002::/16", "6to4"),
    ("fd00:ec2::254/128", "metadata"),
    ("fc00::/7", "unique_local"),
    ("fe80::/10", "link_local"),
    ("fec0::/10", "site_local"),
    ("ff00::/8", "multicast"),
)
_NETWORKS = tuple((ipaddress.ip_network(n), reason) for n, reason in _BLOCKED_NETWORKS)

_NUMERIC_LABEL = re.compile(r"^(?:0[xX][0-9a-fA-F]*|[0-9]+)$")
_USERINFO = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*://)[^/?#@]*@")


class Resolver(Protocol):
    """名前 → IP アドレス（文字列）。テストは Fake を注入する。"""

    async def resolve(self, host: str, port: int) -> Sequence[str]: ...


class SystemResolver:
    """OS の名前解決（getaddrinfo）。DNS の失敗は ``ResolutionError``。"""

    async def resolve(self, host: str, port: int) -> Sequence[str]:
        loop = asyncio.get_running_loop()
        try:
            infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise ResolutionError(f"dns lookup failed ({type(exc).__name__})") from None
        seen: dict[str, None] = {}
        for info in infos:
            seen[str(info[4][0])] = None
        return list(seen)


@dataclass(frozen=True, slots=True)
class GuardedTarget:
    """検査済みの取得先。``addresses`` の IP へだけ接続する（再解決しない）。"""

    #: 正規化した URL（scheme・host は小文字/ASCII、fragment なし）。userinfo は無い
    url: str
    scheme: str
    host: str
    port: int
    addresses: tuple[str, ...]

    @property
    def host_header(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return host if self.port == DEFAULT_PORTS[self.scheme] else f"{host}:{self.port}"


def redact_url(url: str) -> str:
    """ログ・結果に出す URL から userinfo を伏せる。"""
    return _USERINFO.sub(r"\1***@", url)


def blocked_address_reason(address: str) -> str | None:
    """内部・非公開のアドレスなら理由コード、公開なら ``None``。書き方は正規化済みの前提。

    ``ValueError``: アドレスとして読めない。
    """
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address):
        mapped = ip.ipv4_mapped
        if mapped is not None:  # ::ffff:a.b.c.d は埋め込まれた IPv4 の規則で判定する
            return blocked_address_reason(str(mapped))
    for network, reason in _NETWORKS:
        if ip.version == network.version and ip in network:
            return reason
    if not ip.is_global:  # 上の表に無い非公開の範囲への保険
        return "not_global"
    return None


def _legacy_ipv4(host: str) -> ipaddress.IPv4Address | None:
    """inet_aton 流の書き方（10 進・16 進・8 進・省略形）を読む。数字だけの名前なら ``None``。"""
    labels = host.split(".")
    if not all(_NUMERIC_LABEL.match(label) for label in labels):
        return None
    if len(labels) > 4:
        raise UrlNotAllowed("bad_ip_notation")
    values: list[int] = []
    for label in labels:
        try:
            if label[:2].lower() == "0x":
                values.append(int(label[2:], 16))
            elif len(label) > 1 and label.startswith("0"):
                values.append(int(label, 8))
            else:
                values.append(int(label, 10))
        except ValueError:
            raise UrlNotAllowed("bad_ip_notation") from None
    *leading, final = values
    if any(v > 255 for v in leading) or final >= 256 ** (5 - len(labels)):
        raise UrlNotAllowed("bad_ip_notation")
    number = final
    for index, part in enumerate(leading):
        number += part << (8 * (3 - index))
    return ipaddress.IPv4Address(number)


def _split(url: str) -> tuple[str, str, int, str, str]:
    """``(scheme, host, port, path, query)`` へ。構文の違反は ``UrlNotAllowed``。"""
    if not url or len(url) > MAX_URL_LENGTH:
        raise UrlNotAllowed("invalid_url")
    if any(ord(c) <= 0x20 or ord(c) == 0x7F or c == "\\" for c in url):
        raise UrlNotAllowed("invalid_url")
    try:
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        if scheme not in ALLOWED_SCHEMES:
            raise UrlNotAllowed("scheme_not_allowed")
        if "@" in parts.netloc:
            raise UrlNotAllowed("userinfo")
        raw_host = parts.hostname
        raw_port = parts.port
    except ValueError:
        raise UrlNotAllowed("invalid_url") from None
    if not raw_host:
        raise UrlNotAllowed("empty_host")
    if raw_port is not None and not 0 < raw_port <= 65535:
        raise UrlNotAllowed("invalid_port")
    if "%" in raw_host:
        raise UrlNotAllowed("invalid_host")
    host = raw_host.rstrip(".")
    if not host:
        raise UrlNotAllowed("empty_host")
    if ":" not in host:  # IPv6 以外は IDNA で ASCII へ（全角数字・Unicode のドットもここで正規化）
        try:
            host = host.encode("idna").decode("ascii").lower().rstrip(".")
        except UnicodeError:
            raise UrlNotAllowed("invalid_host") from None
        if not host:
            raise UrlNotAllowed("empty_host")
    port = raw_port if raw_port is not None else DEFAULT_PORTS[scheme]
    return scheme, host, port, parts.path, parts.query


class UrlGuard:
    """URL を検査して ``GuardedTarget`` を返す。取得はこの結果の IP だけへ行う。"""

    def __init__(self, resolver: Resolver | None = None) -> None:
        self._resolver: Resolver = resolver or SystemResolver()

    async def check(self, url: str) -> GuardedTarget:
        scheme, host, port, path, query = _split(url)
        literal = self._literal_address(host)
        if literal is not None:
            addresses = self._verified([literal])
        else:
            if host in BLOCKED_NAMES or host.endswith(BLOCKED_SUFFIXES):
                raise UrlNotAllowed("blocked_name")
            if "." not in host:
                raise UrlNotAllowed("single_label_host")
            addresses = self._verified(await self._resolve(host, port))
        netloc = f"[{host}]" if ":" in host else host
        if port != DEFAULT_PORTS[scheme]:
            netloc = f"{netloc}:{port}"
        return GuardedTarget(
            url=urlunsplit((scheme, netloc, path or "/", query, "")),
            scheme=scheme,
            host=host,
            port=port,
            addresses=addresses,
        )

    @staticmethod
    def _literal_address(host: str) -> str | None:
        """IP リテラル（あらゆる書き方）なら正規化した文字列、名前なら ``None``。"""
        if ":" in host:
            try:
                return str(ipaddress.IPv6Address(host))
            except ValueError:
                raise UrlNotAllowed("invalid_host") from None
        legacy = _legacy_ipv4(host)
        return str(legacy) if legacy is not None else None

    async def _resolve(self, host: str, port: int) -> list[str]:
        answers = list(await self._resolver.resolve(host, port))
        if not answers:
            raise ResolutionError("dns lookup returned no address")
        return answers

    @staticmethod
    def _verified(addresses: Sequence[str]) -> tuple[str, ...]:
        """全アドレスが公開であること。1 つでも違えば全体を拒否し、接続先には正規化した形を返す。"""
        reasons: list[str | None] = []
        normalized: list[str] = []
        for address in addresses:
            try:
                ip = ipaddress.ip_address(address)
            except ValueError:
                raise ResolutionError("dns answer is not an ip address") from None
            reasons.append(blocked_address_reason(address))
            mapped = getattr(ip, "ipv4_mapped", None)
            normalized.append(str(mapped) if mapped is not None else str(ip))
        blocked = [r for r in reasons if r is not None]
        if blocked:
            mixed = len(blocked) != len(reasons)
            raise UrlNotAllowed("mixed_resolution" if mixed else "blocked_ip")
        return tuple(dict.fromkeys(normalized))
