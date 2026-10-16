"""Alembic を同じ設定で走らせても、既存のロガーを壊さない。

env.py の ``fileConfig`` は既定で ``disable_existing_loggers=True`` なので、
テストから upgrade すると import 済みモジュールのロガーが無効化され、
caplog に頼る別のテストが実行順に依存して落ちていた（flaky の原因）。
"""

from __future__ import annotations

import logging
import pathlib

from alembic import command
from alembic.config import Config

REPO = pathlib.Path(__file__).resolve().parents[2]


def _config(url: str) -> Config:
    config = Config(str(REPO / "alembic.ini"))
    config.set_main_option("script_location", str(REPO / "infrastructure" / "db" / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    return config


def test_upgrade_does_not_disable_existing_loggers(tmp_path: pathlib.Path) -> None:
    logger = logging.getLogger("tests.contract.migration_logging_probe")
    logger.disabled = False
    command.upgrade(_config(f"sqlite:///{tmp_path / 'a.db'}"), "head")
    assert logger.disabled is False


def test_upgrade_with_configure_logger_off_keeps_root_handlers(tmp_path: pathlib.Path) -> None:
    root = logging.getLogger()
    before = list(root.handlers)
    level = root.level
    config = _config(f"sqlite:///{tmp_path / 'b.db'}")
    config.attributes["configure_logger"] = False
    command.upgrade(config, "head")
    assert list(root.handlers) == before
    assert root.level == level
