"""deploy/logging/scripts/volume-guard.sh: Fluent Bit の前に毎回走る one-shot の判定（I-28）。

位置 DB を消して読み直す（I-26 の緩和策）には、位置 DB 無し＋``read_from_head=true`` を明示的に許す
読み直しモード（``AVP_LOG_REREAD=yes``）が要る。既定のままでは導入手順の誤りとして止め続ける。
理由は docs/testing/logging-platform-rationale.md §9。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy" / "logging" / "scripts" / "volume-guard.sh"


def _guard(
    tmp_path: Path, *, sentinel: bool, db: bool, **env: str
) -> subprocess.CompletedProcess[str]:
    state = tmp_path / "fb-state"
    containers = tmp_path / "containers"
    state.mkdir(exist_ok=True)
    containers.mkdir(exist_ok=True)
    if sentinel:
        (state / ".avp-logging-sentinel").touch()
    if db:
        (state / "tail.db").touch()
    full_env = {
        "PATH": os.environ["PATH"],
        "AVP_GUARD_STATE_DIR": str(state),
        "AVP_GUARD_CONTAINERS_DIR": str(containers),
        **env,
    }
    return subprocess.run(
        ["bash", str(SCRIPT)], env=full_env, capture_output=True, text=True, check=False
    )


def test_normal_start_with_a_position_db(tmp_path: Path) -> None:
    assert _guard(tmp_path, sentinel=True, db=True).returncode == 0


def test_refuses_an_empty_volume(tmp_path: Path) -> None:
    res = _guard(tmp_path, sentinel=False, db=True)
    assert res.returncode == 1 and "security-init" in res.stderr


def test_refuses_read_from_head_without_a_position_db_by_default(tmp_path: Path) -> None:
    """導入時の誤り（既存の大きなログを全量読む）を止める。既定は read_from_head=true。"""
    res = _guard(tmp_path, sentinel=True, db=False)
    assert res.returncode == 1 and "AVP_LOG_READ_FROM_HEAD=false" in res.stderr
    assert "AVP_LOG_REREAD=yes" in res.stderr, "読み直したいときの方法も示す"


def test_first_install_without_a_position_db(tmp_path: Path) -> None:
    assert _guard(tmp_path, sentinel=True, db=False, AVP_LOG_READ_FROM_HEAD="false").returncode == 0


@pytest.mark.parametrize("value", ["yes"])
def test_explicit_reread_mode_allows_reading_from_the_head(tmp_path: Path, value: str) -> None:
    """位置 DB を消して読み直す（I-26 の緩和策、runbook §7）。明示したときだけ通す。"""
    res = _guard(tmp_path, sentinel=True, db=False, AVP_LOG_REREAD=value)
    assert res.returncode == 0
    assert "読み直し" in res.stdout + res.stderr


@pytest.mark.parametrize("value", ["", "true", "1", "YES "])
def test_reread_needs_the_exact_word(tmp_path: Path, value: str) -> None:
    assert _guard(tmp_path, sentinel=True, db=False, AVP_LOG_REREAD=value).returncode == 1


def test_reread_mode_is_refused_while_read_from_head_is_false(tmp_path: Path) -> None:
    """読み直しを頼んだのに末尾から読むと、黙って何も読み直さない。"""
    res = _guard(
        tmp_path, sentinel=True, db=False, AVP_LOG_REREAD="yes", AVP_LOG_READ_FROM_HEAD="false"
    )
    assert res.returncode == 1


def test_reread_with_an_existing_db_is_refused(tmp_path: Path) -> None:
    """位置 DB が残っていれば読み直しにならない（消し忘れ）。"""
    res = _guard(tmp_path, sentinel=True, db=True, AVP_LOG_REREAD="yes")
    assert res.returncode == 1 and "tail.db" in res.stderr
