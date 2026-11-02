"""選曲は Commons に触れない（ADR-0087 §5 / INV-153）: ネットワークを断った状態でも、
カタログ（DB）だけで選曲・保存・確定が進む。受入れ検査の障害注入（Commons 不通）の代わりに、
socket の接続そのものを失敗させて「選曲経路が接続を試みない」ことを固定する。
"""

from __future__ import annotations

import socket

import pytest

from contracts.music_selection import DEFAULT_BRAND_POLICY
from domain.music.auto_select import select_for_episode
from domain.music.requirements import derive_requirements
from infrastructure.db.bgm_repositories import SqlBgmCatalogRepository
from tests.support.bgm_auto import EPISODE, entry_for, loud_track, make_script, make_storyboard
from tests.support.bgm_catalog import NOW


async def test_selection_and_persistence_never_open_a_network_connection(
    session_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: list[object] = []

    def refuse(self, address, *a, **k):  # noqa: ANN001
        if self.family in (socket.AF_INET, socket.AF_INET6):
            attempts.append(address)
            raise OSError("network is down (Commons unavailable)")
        return orig(self, address, *a, **k)

    orig = socket.socket.connect
    monkeypatch.setattr(socket.socket, "connect", refuse)

    script = make_script()
    req = derive_requirements(
        script, make_storyboard(script), fmt="shorts", brand=DEFAULT_BRAND_POLICY
    )
    decision = select_for_episode(
        EPISODE, req, [entry_for(loud_track("bgm-a", "a"))], {}, NOW, brand=DEFAULT_BRAND_POLICY
    )
    repo = SqlBgmCatalogRepository(session_factory)
    saved = await repo.save_selection_once(decision, decided_at=NOW)
    assert saved.track_id == "bgm-a"
    assert (await repo.finalize_selection(saved, finalized_at=NOW)).track_id == "bgm-a"
    assert attempts == []
