"""paid_budget_setting の出所は repo の .env（deploy が適用する値）。ADR-0062 / INV-85。

各テストが存在する理由は docs/testing/release-reservation-waiver-rationale.md。
"""

from __future__ import annotations

import pathlib

import pytest

from infrastructure.release.collectors import CollectorUnavailable, RealCollectors

KEY = "FAL_MONTHLY_BUDGET_USD"


def _collectors(tmp_path: pathlib.Path, env_text: str | None) -> RealCollectors:
    if env_text is not None:
        (tmp_path / ".env").write_text(env_text, encoding="utf-8")
    return RealCollectors(tmp_path)


def test_dotenv_value_is_the_source(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(KEY, raising=False)
    assert _collectors(tmp_path, f"{KEY}=0\n").paid_budget_setting() == "0"


def test_process_env_alone_is_not_the_source(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """.env に無く os.environ だけが 0 でも、deploy が適用する値ではないので採らない。"""
    monkeypatch.setenv(KEY, "0")
    with pytest.raises(CollectorUnavailable):
        _collectors(tmp_path, "OTHER=1\n").paid_budget_setting()


def test_matching_process_env_is_accepted_after_quote_handling(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(KEY, " 0 ")
    assert _collectors(tmp_path, f'{KEY}="0"\n').paid_budget_setting() == "0"


@pytest.mark.parametrize("env_value", ["5", "", "0.5"])
def test_process_env_that_differs_from_dotenv_is_unavailable(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, env_value: str
) -> None:
    monkeypatch.setenv(KEY, env_value)
    with pytest.raises(CollectorUnavailable):
        _collectors(tmp_path, f"{KEY}=0\n").paid_budget_setting()
