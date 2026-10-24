"""Codex model の事前診断（純粋関数。ADR-0058 / INV-75）。

I/O をしない。入力は「設定された model」「許可リスト」「ローカル catalog（models_cache.json）の
写し」だけで、prompt を送らない（= ChatGPT の利用枠を使わない）。

**この関数は「使える」と証明できない。** 証明できるのは「必ず失敗する構成」（catalog に無い・許可
リストに無い・固定が必須なのに未固定）だけ。catalog にあってもサーバーに拒否される model が
実在した（gpt-6-sol、2026-10）ので、``OK`` は所有者が実測して許可リストに書いた model だけに出す。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from contracts.codex_models import (
    BLOCKING_VERDICTS,
    CATALOG_MAX_AGE_HOURS,
    REQUIRE_PINNED_ENV,
    CodexModelVerdict,
)


@dataclass(frozen=True, slots=True)
class CatalogSnapshot:
    slugs: frozenset[str]
    fetched_at: datetime | None


@dataclass(frozen=True, slots=True)
class CodexModelCheck:
    verdict: CodexModelVerdict
    model: str | None
    source: str | None
    message: str

    @property
    def blocking(self) -> bool:
        return self.verdict in BLOCKING_VERDICTS


def _catalog_usable(catalog: CatalogSnapshot | None, now: datetime) -> bool:
    if catalog is None or not catalog.slugs or catalog.fetched_at is None:
        return False
    return now - catalog.fetched_at <= timedelta(hours=CATALOG_MAX_AGE_HOURS)


def evaluate_codex_model(
    *,
    model_setting: str,
    config_model: str | None,
    allowed: frozenset[str],
    require_pinned: bool,
    catalog: CatalogSnapshot | None,
    now: datetime,
) -> CodexModelCheck:
    pinned = bool(model_setting)
    model = model_setting if pinned else config_model
    source = "env" if pinned else ("config.toml" if config_model else None)

    if require_pinned and not pinned:
        return CodexModelCheck(
            CodexModelVerdict.NOT_PINNED,
            model,
            source,
            f"{REQUIRE_PINNED_ENV} is set but CODEX_MODEL is empty "
            f"(effective model {model!r} would come from the shared ~/.codex/config.toml)",
        )
    if model is None:
        return CodexModelCheck(
            CodexModelVerdict.UNVERIFIED,
            None,
            None,
            "no model pinned and none readable from config.toml (codex built-in default)",
        )
    if allowed and model not in allowed:
        return CodexModelCheck(
            CodexModelVerdict.NOT_ALLOWED,
            model,
            source,
            f"codex model {model!r} ({source}) is not in CODEX_ALLOWED_MODELS "
            f"({', '.join(sorted(allowed))})",
        )
    if not _catalog_usable(catalog, now):
        return CodexModelCheck(
            CodexModelVerdict.CATALOG_UNREADABLE,
            model,
            source,
            f"model catalog unreadable or stale; cannot confirm {model!r}",
        )
    assert catalog is not None
    if model not in catalog.slugs:
        return CodexModelCheck(
            CodexModelVerdict.NOT_IN_CATALOG,
            model,
            source,
            f"codex model {model!r} ({source}) is not in the account's model catalog "
            f"(models_cache.json); codex would reject it",
        )
    if allowed:  # model in allowed かつ catalog にもある
        return CodexModelCheck(
            CodexModelVerdict.OK, model, source, f"codex model {model!r} is allowed and catalogued"
        )
    return CodexModelCheck(
        CodexModelVerdict.UNVERIFIED,
        model,
        source,
        f"codex model {model!r} is catalogued but not verified (CODEX_ALLOWED_MODELS is empty)",
    )
