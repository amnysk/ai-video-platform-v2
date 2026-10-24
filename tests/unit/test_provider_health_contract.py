"""provider_health 契約の不変量（ADR-0057）。理由は docs/testing/provider-health-rationale.md。"""

from __future__ import annotations

from contracts.provider_health import (
    PROVIDER_FAMILY,
    ProviderAccount,
    ProviderFamily,
)
from contracts.states import ProviderCall


def test_every_fal_ledger_provider_shares_the_fal_account() -> None:
    fal_calls = {c for c in ProviderCall if c.value.startswith("fal_")}
    assert fal_calls, "fal の台帳 provider が無い"
    assert {c: PROVIDER_FAMILY.get(c) for c in fal_calls} == dict.fromkeys(
        fal_calls, ProviderFamily.FAL
    )


def test_codex_and_youtube_are_never_behind_the_fal_breaker() -> None:
    # 残高不足で止めるのは原因アカウントだけ。台本生成・投稿は巻き込まない
    for call in ProviderCall:
        if not call.value.startswith("fal_"):
            assert call not in PROVIDER_FAMILY


def test_account_key_is_family_and_label() -> None:
    assert ProviderAccount(ProviderFamily.FAL).key == "fal:default"
    assert ProviderAccount(ProviderFamily.FAL, "channel-b").key == "fal:channel-b"
