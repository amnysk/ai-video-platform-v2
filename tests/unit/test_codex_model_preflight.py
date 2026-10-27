"""Codex model の事前診断（ADR-0058 / INV-75）。

本物の codex は起動しない・prompt を送らない（INV-18 / AGENTS.md §9）。読むのは
``config.toml`` と ``models_cache.json`` だけで、課金枠（ChatGPT の利用枠）を使わない。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from contracts.codex_models import CodexModelVerdict
from domain.codex_preflight import CatalogSnapshot, CodexModelCheck, evaluate_codex_model
from infrastructure.providers.codex_model_check import read_models_cache, run_codex_preflight

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
FRESH = NOW - timedelta(hours=3)
CATALOG = CatalogSnapshot(
    slugs=frozenset({"gpt-6.1-sol", "gpt-6-sol", "gpt-5.6-terra"}), fetched_at=FRESH
)


def _eval(
    *,
    model_setting: str = "",
    config_model: str | None = None,
    allowed: frozenset[str] = frozenset(),
    require_pinned: bool = False,
    catalog: CatalogSnapshot | None = CATALOG,
) -> CodexModelCheck:
    return evaluate_codex_model(
        model_setting=model_setting,
        config_model=config_model,
        allowed=allowed,
        require_pinned=require_pinned,
        catalog=catalog,
        now=NOW,
    )


def test_model_missing_from_the_catalog_blocks_before_any_prompt() -> None:
    check = _eval(model_setting="gpt-5.5")
    assert check.verdict is CodexModelVerdict.NOT_IN_CATALOG
    assert check.blocking
    assert "gpt-5.5" in check.message


def test_a_catalogued_model_is_never_reported_as_verified_without_an_allowlist() -> None:
    """gpt-6-sol は catalog にあるのにサーバーが 400 で拒否した（2026-10 の実害）。"""
    check = _eval(model_setting="gpt-6-sol")
    assert check.verdict is CodexModelVerdict.UNVERIFIED
    assert not check.blocking


def test_a_model_outside_the_allowlist_blocks() -> None:
    check = _eval(config_model="gpt-6-sol", allowed=frozenset({"gpt-6.1-sol"}))
    assert check.verdict is CodexModelVerdict.NOT_ALLOWED
    assert check.blocking
    assert check.source == "config.toml"


def test_a_model_in_both_allowlist_and_catalog_is_ok() -> None:
    check = _eval(model_setting="gpt-6.1-sol", allowed=frozenset({"gpt-6.1-sol"}))
    assert check.verdict is CodexModelVerdict.OK
    assert not check.blocking
    assert check.source == "env"


def test_allowlist_member_missing_from_catalog_still_blocks() -> None:
    check = _eval(model_setting="gpt-9", allowed=frozenset({"gpt-9"}))
    assert check.verdict is CodexModelVerdict.NOT_IN_CATALOG


def test_unpinned_model_from_the_shared_config_is_a_warning_until_pinning_is_required() -> None:
    soft = _eval(config_model="gpt-6-sol")
    assert soft.verdict is CodexModelVerdict.UNVERIFIED
    assert soft.source == "config.toml"
    hard = _eval(config_model="gpt-6-sol", require_pinned=True)
    assert hard.verdict is CodexModelVerdict.NOT_PINNED
    assert hard.blocking


def test_unreadable_or_stale_catalog_never_blocks_and_never_verifies() -> None:
    assert _eval(model_setting="x", catalog=None).verdict is CodexModelVerdict.CATALOG_UNREADABLE
    stale = CatalogSnapshot(slugs=frozenset({"y"}), fetched_at=NOW - timedelta(days=30))
    check = _eval(model_setting="x", catalog=stale)
    assert check.verdict is CodexModelVerdict.CATALOG_UNREADABLE
    assert not check.blocking


def test_no_model_anywhere_is_unverified_not_ok() -> None:
    check = _eval()
    assert check.verdict is CodexModelVerdict.UNVERIFIED
    assert check.model is None


def test_read_models_cache_parses_slugs_and_fetch_time(tmp_path: Path) -> None:
    path = tmp_path / "models_cache.json"
    path.write_text(
        json.dumps(
            {
                "fetched_at": "2026-10-24T15:45:22.177816474Z",
                "models": [{"slug": "gpt-6.1-sol"}, {"slug": "gpt-6-sol"}],
            }
        )
    )
    snap = read_models_cache(path)
    assert snap is not None
    assert snap.slugs == frozenset({"gpt-6.1-sol", "gpt-6-sol"})
    assert snap.fetched_at == datetime(2026, 10, 24, 15, 45, 22, 177816, tzinfo=UTC)
    assert read_models_cache(tmp_path / "missing.json") is None
    path.write_text("{broken")
    assert read_models_cache(path) is None


def test_run_preflight_reads_files_only(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text('model = "gpt-6-sol"\n')
    (tmp_path / "models_cache.json").write_text(
        json.dumps({"fetched_at": FRESH.isoformat(), "models": [{"slug": "gpt-6.1-sol"}]})
    )
    check = run_codex_preflight(
        model_setting="",
        allowed_raw="",
        require_pinned=False,
        codex_home=tmp_path,
        now=NOW,
    )
    assert check.verdict is CodexModelVerdict.NOT_IN_CATALOG
    assert check.model == "gpt-6-sol"
