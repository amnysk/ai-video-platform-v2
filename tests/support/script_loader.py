"""``scripts/`` 以下のハイフン入りファイル名スクリプトをテストから読み込む。

``scripts/youtube-oauth.py`` のようなファイルはハイフンを含むため通常の
``import`` 文では読み込めない。``importlib.util.spec_from_file_location`` +
``exec_module`` で読み込む必要があるが、素朴にやると読み込んだモジュールが
``sys.modules`` に登録されたまま残る。

これが問題になる: Temporal SDK の ``Worker(...)`` は ``build_id`` を
明示しないと ``load_default_build_id()`` を呼び、これは **``sys.modules`` を
丸ごと走査して各モジュールの ``loader.get_code()`` を呼び、bytecode を
ハッシュして worker の build id を導出する**
（``temporalio/worker/_worker.py``）。この走査は ``sys.dont_write_bytecode``
をロード時にどれだけ丁寧にトグルしても防げない ―― 走査が起きるのは
ロードよりずっと後（無関係な他のテストの実行中）であり、その時点では
フラグはとっくに元の値へ戻っている。``SourceFileLoader.get_code()`` は
その走査の中で ``scripts/__pycache__/`` へ ``.pyc`` を書き込み、
``tests/architecture/test_no_live_calls.py`` の
``test_scripts_mention_youtube_endpoints_only_in_the_consent_script`` が
それを ``scripts/`` の全ファイルスキャンで拾って INV-18 違反と誤検知する。

対策はロード時のフラグ操作ではなく、**ロードが終わったら sys.modules から
取り除く**こと。呼び出し側はこの関数が返すモジュールオブジェクトを直接
使うので、``sys.modules`` に見つかる必要はない。登録を残さなければ、
Temporal のように ``sys.modules`` を無差別に走査するコードからそもそも
見えなくなる。
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
from types import ModuleType


def load_script_module(module_name: str, script_path: pathlib.Path) -> ModuleType:
    """``script_path`` を ``module_name`` として読み込み、実行済みモジュールを返す。

    読み込み中は ``sys.dont_write_bytecode`` を立てて ``.pyc`` を書かせない
    （ロードそのものからの汚染を防ぐ）。読み込み後は ``sys.modules`` から
    取り除く（ロード後に無関係なコードが走査して汚染するのを防ぐ）。
    どちらか片方だけでは不十分 -- 上のモジュール docstring を参照。
    """
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    previous, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
        sys.modules.pop(module_name, None)
    return module
