"""integration テストが本番スタックへ繋がる事故を止める静的規則（ADR-0079 / INV-121）。

本番 compose（project ``avp2``）は同じホストで Temporal / PostgreSQL / MinIO / API を
``localhost``・LAN IP の既定ポート（7233 / 5432 / 9000）で公開している。テストが
``localhost:7233`` を直書きする・``TEMPORAL_ADDRESS`` を本番へ向けると、namespace や bucket の
guard（tests/support/temporal.py / minio.py）は通っても**本番サーバそのもの**に繋がる。

規則は**許可リスト**（隔離の積極的な宣言）で、拒否リストではない。宣言が無い・綴りが違う・
想定外の値のとき、接続先が1つでも設定されていれば例外（fail closed）。宣言は次の2つだけ:

- ``apptest:<project>``: ``deploy/logging/test/run-e2e.sh`` の隔離スタック。接続先は内部 network の
  サービス名（``temporal`` / ``postgres`` / ``minio``）だけ。ホスト自身（loopback・ホストの IP に
  解決される名前）は拒否する。project は ``avp2-oslog-c*`` だけ（本番 ``avp2`` は不可）。
- ``ci-ephemeral``: GitHub Actions の使い捨てサービス。``GITHUB_ACTIONS=true`` が同時に要り、
  接続先は loopback だけ。

動的な確認（本番 Temporal に Schedule ``avp-daily-episode`` が在るか）は
``tests/support/temporal.py::assert_temporal_is_not_production`` が行う。
"""

from __future__ import annotations

import contextlib
import re
import socket
from collections.abc import Callable, Mapping
from urllib.parse import urlsplit

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

ENV_ISOLATION = "AVP_INTEGRATION_ISOLATION"
ISOLATION_CI = "ci-ephemeral"
ISOLATION_APPTEST_PREFIX = "apptest:"
#: run-e2e.sh が許す project 名と同じ形（本番 ``avp2`` は含まない）
_APPTEST_PROJECT = re.compile(r"avp2-oslog-c(?:[-_0-9a-z]*)")
#: 隔離スタックの内部 network に在るサービス名（compose.apptest.yaml）
APPTEST_SERVICE_HOSTS = frozenset({"temporal", "postgres", "minio"})
LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})
#: 本番 compose が host に公開しているポート（記述用。判定は宣言が主）
PRODUCTION_PUBLISHED_PORTS = {"temporal": 7233, "postgres": 5432, "minio": 9000}


class ProductionTargetError(RuntimeError):
    """integration テストの接続先が、隔離されていると確認できない。"""


Resolver = Callable[[str], list[str]]


def _resolve(host: str) -> list[str]:
    try:
        return sorted({str(info[4][0]) for info in socket.getaddrinfo(host, None)})
    except OSError:
        return []


def host_local_addresses() -> frozenset[str]:
    """このマシン自身の IP（パケットは送らない）。LAN IP に解決される名前もホストとみなす。"""
    found: set[str] = set()
    with contextlib.suppress(OSError):
        found.update(str(info[4][0]) for info in socket.getaddrinfo(socket.gethostname(), None))
    for family, probe in ((socket.AF_INET, "192.0.2.1"), (socket.AF_INET6, "2001:db8::1")):
        try:
            with socket.socket(family, socket.SOCK_DGRAM) as sock:
                sock.connect((probe, 9))  # UDP: 経路を選ぶだけで送信しない
                found.add(sock.getsockname()[0])
        except OSError:
            pass
    return frozenset(found)


def _is_loopback_address(address: str) -> bool:
    return address in {"::1", "0.0.0.0"} or address.startswith("127.")


def _is_this_host(host: str, local: frozenset[str], resolver: Resolver) -> bool:
    name = host.lower().rstrip(".")
    if name in LOOPBACK_NAMES or _is_loopback_address(name) or name in local:
        return True
    return any(_is_loopback_address(a) or a in local for a in resolver(name))


def _hostport(address: str) -> tuple[str, int]:
    host, sep, port = address.strip().rpartition(":")
    if not sep or not host or not port.isdigit():
        raise ProductionTargetError(f"接続先 {address!r} を host:port として解釈できない")
    return host.strip("[]"), int(port)


def _targets(environ: Mapping[str, str]) -> list[tuple[str, str, int]]:
    """設定されている接続先を ``(名前, host, port)`` にする。解釈できなければ例外。"""
    found: list[tuple[str, str, int]] = []
    if environ.get("TEMPORAL_ADDRESS"):
        host, port = _hostport(environ["TEMPORAL_ADDRESS"])
        found.append(("TEMPORAL_ADDRESS", host, port))
    for key in ("TEST_DATABASE_URL", "DATABASE_URL"):
        if environ.get(key):
            try:
                url = make_url(environ[key])
            except ArgumentError as exc:
                raise ProductionTargetError(f"{key} を解釈できない") from exc
            if not url.host:
                raise ProductionTargetError(f"{key} に host が無い（libpq の環境変数に左右される）")
            found.append((key, url.host, url.port or 5432))
    if environ.get("MINIO_ENDPOINT"):
        raw = environ["MINIO_ENDPOINT"]
        parts = urlsplit(raw if "://" in raw else f"http://{raw}")
        if not parts.hostname:
            raise ProductionTargetError("MINIO_ENDPOINT を解釈できない")
        found.append(("MINIO_ENDPOINT", parts.hostname, parts.port or 80))
    return found


def assert_isolated_targets(
    environ: Mapping[str, str],
    *,
    local_addresses: frozenset[str] | None = None,
    resolver: Resolver | None = None,
) -> None:
    """接続先が1つでも設定されているなら、隔離の宣言と接続先の整合を要求する。"""
    targets = _targets(environ)
    if not targets:
        return  # インフラ無し（テストは skip になる）
    local = host_local_addresses() if local_addresses is None else local_addresses
    resolve = resolver or _resolve
    declared = environ.get(ENV_ISOLATION, "")
    names = ", ".join(f"{name}={host}:{port}" for name, host, port in targets)

    if declared == ISOLATION_CI:
        if environ.get("GITHUB_ACTIONS") != "true":
            raise ProductionTargetError(
                f"{ENV_ISOLATION}={ISOLATION_CI} は GitHub Actions の使い捨てサービス専用"
                "（GITHUB_ACTIONS=true が無い）。本番と同じホストでは使えない"
            )
        for name, host, _port in targets:
            if host.lower() not in LOOPBACK_NAMES and not _is_loopback_address(host):
                raise ProductionTargetError(f"CI の接続先は loopback だけ: {name}={host}")
        return

    if declared.startswith(ISOLATION_APPTEST_PREFIX):
        project = declared[len(ISOLATION_APPTEST_PREFIX) :]
        if not _APPTEST_PROJECT.fullmatch(project):
            raise ProductionTargetError(
                f"{ENV_ISOLATION} の project {project!r} は avp2-oslog-c* ではない"
                "（本番 project avp2 は隔離スタックではない）"
            )
        for name, host, _port in targets:
            if host.lower() not in APPTEST_SERVICE_HOSTS:
                raise ProductionTargetError(
                    f"隔離スタックの接続先は内部サービス名 {sorted(APPTEST_SERVICE_HOSTS)} だけ: "
                    f"{name}={host}"
                )
            if _is_this_host(host, local, resolve):
                raise ProductionTargetError(
                    f"{name}={host} がこのホスト自身に解決される（本番の公開ポートの可能性）"
                )
        return

    raise ProductionTargetError(
        "integration テストの接続先が設定されているが、隔離の宣言が無い（または不正）: "
        f"{names}。本番スタックは同じホストの localhost:7233 / 5432 / 9000 で公開されている。"
        "integration は `deploy/logging/test/run-e2e.sh`（隔離スタック）か CI でだけ実行する"
        f"（{ENV_ISOLATION} は run-e2e.sh / ci.yml が設定する。手で設定しない）"
    )


__all__ = [
    "APPTEST_SERVICE_HOSTS",
    "ENV_ISOLATION",
    "ISOLATION_APPTEST_PREFIX",
    "ISOLATION_CI",
    "PRODUCTION_PUBLISHED_PORTS",
    "ProductionTargetError",
    "assert_isolated_targets",
    "host_local_addresses",
]
