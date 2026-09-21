"""SSRF 規則（ADR-0031 §7）。実 DNS・実ネットワークには出ない（Fake resolver）。

守るもの: 「検索結果や本文にあった URL を辿って、内部ネットワーク・メタデータ・ローカルの
ファイルへ到達しない」こと。拒否は URL の**書き方**（10 進/16 進/8 進、IPv4-mapped、IDN、
userinfo）と**名前解決の結果**（複数 A のうち 1 つでも private なら全体を拒否）の両方で行う。
"""

from __future__ import annotations

import pytest

from infrastructure.research.errors import ResolutionError, UrlNotAllowed
from infrastructure.research.url_guard import (
    GuardedTarget,
    UrlGuard,
    blocked_address_reason,
    redact_url,
)
from tests.support.fake_resolver import FakeResolver

PUBLIC = "93.184.216.34"


def _guard(**table: list[str]) -> UrlGuard:
    return UrlGuard(FakeResolver({"example.com": [PUBLIC], **table}))


BLOCKED_URLS = [
    # loopback / unspecified
    "http://localhost/",
    "http://LOCALHOST/",
    "http://localhost./",
    "http://foo.localhost/",
    "http://127.0.0.1/",
    "http://127.255.255.254/",
    "http://[::1]/",
    "http://[::]/",
    "http://0.0.0.0/",
    "http://0.0.0.0:8080/",
    # private
    "http://10.0.0.1/",
    "http://10.255.255.255/",
    "http://172.16.0.1/",
    "http://172.31.255.255/",
    "http://192.168.1.1/",
    "http://192.168.0.1:8080/admin",
    # link-local / metadata
    "http://169.254.169.254/latest/meta-data/",
    "http://169.254.0.1/",
    "http://[fd00:ec2::254]/",
    "http://[fe80::1]/",
    "http://metadata.google.internal/computeMetadata/v1/",
    "http://instance-data/latest/",
    # carrier-grade NAT / reserved / multicast / benchmarking
    "http://100.64.0.1/",
    "http://100.127.255.255/",
    "http://240.0.0.1/",
    "http://224.0.0.1/",
    "http://[ff02::1]/",
    "http://198.18.0.1/",
    "http://[fc00::1]/",
    # IPv4-mapped / 6to4 / NAT64 / IPv4-compatible IPv6
    "http://[::ffff:127.0.0.1]/",
    "http://[::ffff:7f00:1]/",
    "http://[::ffff:10.0.0.1]:8080/",
    "http://[::ffff:a9fe:a9fe]/",
    "http://[2002:7f00:1::]/",
    "http://[2002:a9fe:a9fe::1]/",
    "http://[64:ff9b::7f00:1]/",
    "http://[::127.0.0.1]/",
    # 10 進 / 16 進 / 8 進 / 省略形の IPv4 表記
    "http://2130706433/",
    "http://0x7f000001/",
    "http://0x7f.1/",
    "http://0x7f.0x0.0x0.0x1/",
    "http://0177.0.0.1/",
    "http://017700000001/",
    "http://127.1/",
    "http://127.0.1/",
    "http://0/",
    "http://2852039166/",
    "http://127.0.0.1./",
    # 全角・Unicode ドット・大文字（IDNA の正規化で数字になる）
    "http://１２７.０.０.１/",
    "http://127。0。0。1/",
    "HTTP://LOCALHOST/",
    # 内部向けの名前
    "http://printer.local/",
    "http://db.internal/",
    "http://app.localdomain/",
    "http://router.home.arpa/",
    "http://intranet/",
    # userinfo
    "http://user:pass@example.com/",
    "http://user@example.com/",
    "http://:pw@example.com/",
    "http://example.com@127.0.0.1/",
    "https://token@example.com/x",
    # scheme
    "file:///etc/passwd",
    "ftp://example.com/",
    "gopher://example.com/",
    "data:text/plain,hi",
    "javascript:alert(1)",
    "//example.com/x",
    "example.com/x",
    # 壊れた URL
    "http://",
    "http:///path",
    "http://exa mple.com/",
    "http://example.com/\n",
    "http://example.com\\@127.0.0.1/",
    "http://example.com:0/",
    "http://example.com:99999/",
    "http://example.com:abc/",
    "http://999.1.1.1/",
    "http://1.2.3.4.5/",
    "http://[::1%25eth0]/",
    "",
]


@pytest.mark.parametrize("url", BLOCKED_URLS)
async def test_blocked_urls_are_rejected_before_any_connection(url: str) -> None:
    resolver = FakeResolver({"example.com": [PUBLIC]})
    with pytest.raises(UrlNotAllowed):
        await UrlGuard(resolver).check(url)


async def test_rejection_reasons_are_machine_readable_codes() -> None:
    guard = _guard()
    cases = {
        "file:///etc/passwd": "scheme_not_allowed",
        "http://user:pass@example.com/": "userinfo",
        "http://127.0.0.1/": "blocked_ip",
        "http://localhost/": "blocked_name",
        "http://intranet/": "single_label_host",
        "http://2130706433/": "blocked_ip",
    }
    for url, reason in cases.items():
        with pytest.raises(UrlNotAllowed) as caught:
            await guard.check(url)
        assert caught.value.reason == reason, url


async def test_rejection_never_echoes_userinfo() -> None:
    with pytest.raises(UrlNotAllowed) as caught:
        await _guard().check("http://admin:hunter2@example.com/")
    assert "hunter2" not in str(caught.value)
    assert redact_url("http://admin:hunter2@example.com/x?y=1") == "http://***@example.com/x?y=1"


async def test_literal_ip_hosts_are_never_sent_to_the_resolver() -> None:
    resolver = FakeResolver()
    target = await UrlGuard(resolver).check("http://93.184.216.34:8080/x")
    assert resolver.calls == []
    assert target.addresses == ("93.184.216.34",)
    assert target.port == 8080


@pytest.mark.parametrize(
    "resolved",
    [
        ["10.0.0.5"],
        ["127.0.0.1"],
        ["169.254.169.254"],
        ["100.64.1.1"],
        ["::1"],
        ["fd00:ec2::254"],
        ["::ffff:10.0.0.1"],
        ["2002:7f00:1::"],
        [PUBLIC, "10.0.0.5"],
        ["10.0.0.5", PUBLIC],
        [PUBLIC, "::1"],
    ],
    ids=lambda r: ",".join(r),
)
async def test_a_name_that_resolves_to_a_private_address_is_rejected(resolved: list[str]) -> None:
    """公開 IP と private が混在しても全体を拒否する（どれに繋がるかを攻撃者に選ばせない）。"""
    with pytest.raises(UrlNotAllowed) as caught:
        await UrlGuard(FakeResolver({"rebind.example": resolved})).check("http://rebind.example/")
    assert caught.value.reason in {"blocked_ip", "mixed_resolution"}


async def test_public_urls_are_accepted_and_pinned_to_the_checked_address() -> None:
    target = await _guard().check("HTTPS://Example.COM/a/b?c=1#frag")
    assert isinstance(target, GuardedTarget)
    assert target.scheme == "https"
    assert target.host == "example.com"
    assert target.port == 443
    assert target.addresses == (PUBLIC,)
    assert target.url == "https://example.com/a/b?c=1"  # fragment は送らない


async def test_default_ports_and_explicit_ports() -> None:
    guard = _guard()
    assert (await guard.check("http://example.com/")).port == 80
    assert (await guard.check("https://example.com:8443/")).port == 8443


async def test_idn_hosts_are_resolved_by_their_ascii_form() -> None:
    resolver = FakeResolver({"xn--wgv71a119e.example": [PUBLIC]})
    target = await UrlGuard(resolver).check("https://日本語.example/x")
    assert target.host == "xn--wgv71a119e.example"
    assert resolver.calls[0][0] == "xn--wgv71a119e.example"


async def test_public_ipv6_literal_is_allowed() -> None:
    target = await UrlGuard(FakeResolver()).check("http://[2606:2800:220:1:248:1893:25c8:1946]/")
    assert target.addresses == ("2606:2800:220:1:248:1893:25c8:1946",)


async def test_a_mapped_public_address_is_pinned_as_plain_ipv4() -> None:
    target = await UrlGuard(FakeResolver()).check("http://[::ffff:8.8.8.8]/")
    assert target.addresses == ("8.8.8.8",)


async def test_resolution_failure_is_not_a_policy_violation() -> None:
    """名前解決の失敗は通信障害（再試行できる）。UrlNotAllowed（恒久）と混ぜない。"""
    with pytest.raises(ResolutionError):
        await UrlGuard(FakeResolver()).check("http://nxdomain.example/")


async def test_an_empty_resolution_is_a_resolution_error() -> None:
    with pytest.raises(ResolutionError):
        await UrlGuard(FakeResolver({"empty.example": []})).check("http://empty.example/")


async def test_an_unparseable_resolver_answer_is_rejected() -> None:
    with pytest.raises(ResolutionError):
        await UrlGuard(FakeResolver({"weird.example": ["not-an-ip"]})).check(
            "http://weird.example/"
        )


@pytest.mark.parametrize(
    ("address", "blocked"),
    [
        ("93.184.216.34", False),
        ("8.8.8.8", False),
        ("1.1.1.1", False),
        ("2606:4700:4700::1111", False),
        ("172.15.255.255", False),
        ("172.32.0.1", False),
        ("100.63.255.255", False),
        ("100.128.0.1", False),
        ("127.0.0.1", True),
        ("100.64.0.0", True),
        ("100.127.255.255", True),
        ("169.254.169.254", True),
        ("192.0.0.1", True),
        ("192.0.2.1", True),
        ("::ffff:192.168.0.1", True),
        ("::ffff:8.8.8.8", False),
        ("fec0::1", True),
        ("::", True),
    ],
)
def test_blocked_address_reason(address: str, blocked: bool) -> None:
    assert (blocked_address_reason(address) is not None) is blocked
