"""Alembic の head が1本であること。

ブランチを並行で育てる（1 agent = 1 branch）と、別々のブランチが同じ親から
migration を足して head が2本に割れる。merge 後に ``alembic upgrade head`` が
「Multiple head revisions」で落ちる前に、PR の時点で検出する。
"""

from __future__ import annotations

import pathlib

from alembic.config import Config
from alembic.script import ScriptDirectory

REPO = pathlib.Path(__file__).resolve().parents[2]


def test_alembic_has_exactly_one_head() -> None:
    config = Config(str(REPO / "alembic.ini"))
    config.set_main_option("script_location", str(REPO / "infrastructure" / "db" / "migrations"))
    heads = ScriptDirectory.from_config(config).get_heads()
    assert len(heads) == 1, f"multiple alembic heads: {heads}"
