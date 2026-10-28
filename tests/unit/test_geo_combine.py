"""決定論的な地図検証と critic の合流（INV-94、設計書 v3 のテスト T32）。

理由は docs/testing/geo-contracts-rationale.md。
"""

from __future__ import annotations

import itertools

import pytest
from pydantic import ValidationError

from contracts.geo import GEO_VERDICT_ORDER, GeoVerdict, GeoVerification
from domain.geo import combine_verdicts, verify_manifest
from tests.support import fake_gazetteer as fg


def _det(m_over: dict[str, object] | None = None) -> GeoVerification:
    return verify_manifest(fg.manifest(**(m_over or {})), fg.ALL_REFERENCES, fg.GOOD_CLAIMS)


def _blocked() -> GeoVerification:
    # 関ヶ原を東京の位置に描いた誤り（T07）
    return _det({"places": [fg.place(lat=35.6895, lon=139.6917)]})


def test_a_critic_pass_cannot_override_a_deterministic_block() -> None:
    """T32: QualityCritic が誤りを PASS と評価しても BLOCK のまま。"""
    det = _blocked()
    assert det.verdict is GeoVerdict.BLOCK
    combined = combine_verdicts(det, GeoVerdict.PASS)
    assert combined.verdict is GeoVerdict.BLOCK
    assert any(c.name == "critic" and c.outcome is GeoVerdict.PASS for c in combined.checks)


def test_a_critic_pass_cannot_override_a_deterministic_review() -> None:
    det = _det({"borders": "modern"})
    assert det.verdict is GeoVerdict.REVIEW
    assert combine_verdicts(det, GeoVerdict.PASS).verdict is GeoVerdict.REVIEW
    assert combine_verdicts(det, GeoVerdict.WARN).verdict is GeoVerdict.REVIEW


@pytest.mark.parametrize(
    ("det_verdict", "critic"), list(itertools.product(GEO_VERDICT_ORDER, GEO_VERDICT_ORDER))
)
def test_the_combined_verdict_is_never_milder_than_either_input(
    det_verdict: GeoVerdict, critic: GeoVerdict
) -> None:
    """全 16 組み合わせ: 結果 = 厳しい方。どちらよりも緩くならない。"""
    from contracts.geo import GeoCheck

    det = GeoVerification(
        verdict=det_verdict, checks=(GeoCheck(name="d", outcome=det_verdict, detail="x"),)
    )
    result = combine_verdicts(det, critic).verdict
    order = list(GEO_VERDICT_ORDER)
    assert order.index(result) == max(order.index(det_verdict), order.index(critic))


def test_a_stricter_critic_can_tighten_a_pass() -> None:
    det = _det()
    assert det.verdict is GeoVerdict.PASS
    assert combine_verdicts(det, GeoVerdict.BLOCK).verdict is GeoVerdict.BLOCK
    assert combine_verdicts(det, GeoVerdict.REVIEW).verdict is GeoVerdict.REVIEW


def test_the_deterministic_checks_are_kept_unchanged() -> None:
    det = _blocked()
    combined = combine_verdicts(det, GeoVerdict.PASS)
    assert combined.checks[: len(det.checks)] == det.checks


def test_a_score_is_not_accepted_in_place_of_a_verdict() -> None:
    """点数（0.99 など）を verdict に写す経路が無い。"""
    with pytest.raises(TypeError):
        combine_verdicts(_blocked(), 0.99)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        combine_verdicts(_blocked(), "pass")  # type: ignore[arg-type]


def test_a_combined_record_cannot_be_hand_edited_to_pass() -> None:
    """契約側の防衛線: 結合後の記録の verdict だけを PASS に書き換えた記録は作れない。"""
    combined = combine_verdicts(_blocked(), GeoVerdict.PASS)
    with pytest.raises(ValidationError):
        GeoVerification(verdict=GeoVerdict.PASS, checks=combined.checks)
