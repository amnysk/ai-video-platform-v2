"""``tests/support/script_loader.load_script_module`` の回帰テスト。

背景（INV-18 の誤検知の実際の経路）:

``tests/unit/test_youtube_oauth_script.py`` / ``tests/unit/test_check_youtube_analytics.py``
は ``scripts/youtube-oauth.py`` / ``scripts/check-youtube-analytics.py``（ハイフン入りで
``import`` 文が使えない）を ``importlib.util.spec_from_file_location`` +
``exec_module`` で読み込み、``sys.dont_write_bytecode`` を一時的に ``True`` にして
``.pyc`` を書かせないようにしていた。しかしこのガードは ``exec_module`` 呼び出し
そのものだけを覆っており、ロードしたモジュールを ``sys.modules`` に登録したまま
残す実装だった。

full suite を通すと、無関係な ``tests/unit/test_daily_watchdog_workflow.py`` が
``temporalio.worker.Worker(...)`` を ``build_id`` 未指定で construct し、
Temporal SDK の ``load_default_build_id()`` が **``sys.modules`` を丸ごと走査して
各モジュールの ``loader.get_code()`` を呼び、bytecode をハッシュして worker の
build id を導出する**（``temporalio/worker/_worker.py`` の
``load_default_build_id`` / ``_get_module_code``）。この走査の時点では
``sys.dont_write_bytecode`` はとっくに元の値（``False``）へ戻っており、
``SourceFileLoader.get_code()`` は ``scripts/__pycache__/`` へ ``.pyc`` を
書き込む。これを ``tests/architecture/test_no_live_calls.py`` の
``test_scripts_mention_youtube_endpoints_only_in_the_consent_script`` が
``scripts/`` の全ファイルスキャンで拾い、埋め込まれた Google OAuth
エンドポイントの文字列定数を INV-18 違反として誤検知する。

根本原因は「``sys.dont_write_bytecode`` の scope がずれている」ことではなく、
「ロード後にモジュールを ``sys.modules`` に残し続けている」こと。呼び出し側は
戻り値のモジュールオブジェクトを直接使うので、登録を残す理由はない。
``load_script_module`` はロード直後に ``sys.modules`` から取り除くことで、
Temporal のような「``sys.modules`` を無差別に走査するコード」からそもそも
見えなくする。
"""

from __future__ import annotations

import contextlib
import importlib.util
import pathlib
import sys

from tests.support.script_loader import load_script_module


def _scan_like_temporal_load_default_build_id() -> None:
    """``temporalio.worker._worker._get_module_code`` と同じ手口を再現する。

    ``sys.modules`` の全キーについて、その ``__loader__.get_code(name)`` を呼ぶ
    （例外は無視 -- 実装と同じ）。副作用として ``SourceFileLoader`` は
    ``sys.dont_write_bytecode`` が偽なら ``.pyc`` をディスクに書く。
    """
    for name in sorted(sys.modules):
        module = sys.modules.get(name)
        loader = getattr(module, "__loader__", None)
        if loader is None or not hasattr(loader, "get_code"):
            continue
        with contextlib.suppress(Exception):
            loader.get_code(name)


def test_reproduces_the_leak_when_a_naively_loaded_module_stays_in_sys_modules(
    tmp_path: pathlib.Path,
) -> None:
    """再現: helper を使わない素朴な実装は sys.modules に登録したままなので、
    Temporal 型の走査だけで bytecode が書かれてしまう。
    """
    script = tmp_path / "leaky-script.py"
    script.write_text("VALUE = 1\n", encoding="utf-8")
    module_name = "regression_leaky_script"

    spec = importlib.util.spec_from_file_location(module_name, script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    previous, sys.dont_write_bytecode = sys.dont_write_bytecode, False
    try:
        spec.loader.exec_module(module)
        assert module_name in sys.modules  # 素朴な実装はここに残る

        _scan_like_temporal_load_default_build_id()

        assert list((tmp_path / "__pycache__").glob("*.pyc")), (
            "再現に失敗: 素朴な実装では走査で .pyc が書かれるはず"
        )
    finally:
        sys.dont_write_bytecode = previous
        sys.modules.pop(module_name, None)


def test_load_script_module_does_not_leave_the_module_registered(
    tmp_path: pathlib.Path,
) -> None:
    script = tmp_path / "clean-script.py"
    script.write_text("VALUE = 2\n", encoding="utf-8")
    module_name = "regression_clean_script"

    module = load_script_module(module_name, script)

    assert module.VALUE == 2
    assert module_name not in sys.modules


def test_load_script_module_survives_a_temporal_style_scan_without_writing_bytecode(
    tmp_path: pathlib.Path,
) -> None:
    """修正の証明: load_script_module でロードした直後に同じ走査をかけても
    もう sys.modules に無いので拾われず、.pyc は書かれない。
    """
    script = tmp_path / "safe-script.py"
    script.write_text("VALUE = 3\n", encoding="utf-8")
    module_name = "regression_safe_script"

    previous, sys.dont_write_bytecode = sys.dont_write_bytecode, False
    try:
        module = load_script_module(module_name, script)
        assert module.VALUE == 3

        _scan_like_temporal_load_default_build_id()

        assert not (tmp_path / "__pycache__").exists()
    finally:
        sys.dont_write_bytecode = previous


def test_load_script_module_still_prevents_bytecode_during_the_load_itself(
    tmp_path: pathlib.Path,
) -> None:
    """ロード実行そのものでも .pyc を残さない（元々のガードの役目も維持する）。"""
    script = tmp_path / "quiet-script.py"
    script.write_text("VALUE = 4\n", encoding="utf-8")
    module_name = "regression_quiet_script"

    previous, sys.dont_write_bytecode = sys.dont_write_bytecode, False
    try:
        module = load_script_module(module_name, script)
        assert module.VALUE == 4
        assert not (tmp_path / "__pycache__").exists()
    finally:
        sys.dont_write_bytecode = previous
