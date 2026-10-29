"""INV-102: 決定論 BLOCK/REVIEW は LLM の判定で緩まない（design v3 T32）。"""

from __future__ import annotations

import itertools

import pytest

from contracts.quality import QualityCheck, QualityGate, QualityReason, QualityVerdict
from domain.quality import aggregate

V = QualityVerdict


def _check(verdict: QualityVerdict, *, deterministic: bool, gate: QualityGate | None = None):
    return QualityCheck(
        gate=gate or (QualityGate.GEO if deterministic else QualityGate.CRITIC),
        verdict=verdict,
        reason_code=QualityReason.OK if verdict is V.PASS else QualityReason.GEO_MISMATCH,
        detail="d",
        deterministic=deterministic,
    )


def det(v: QualityVerdict) -> QualityCheck:
    return _check(v, deterministic=True)


def llm(v: QualityVerdict) -> QualityCheck:
    return _check(v, deterministic=False)


def test_all_pass_is_pass() -> None:
    assert aggregate([det(V.PASS), det(V.PASS)]) is V.PASS


def test_warn_is_reported_but_does_not_block() -> None:
    assert aggregate([det(V.PASS), det(V.WARN)]) is V.WARN


def test_review_beats_warn() -> None:
    assert aggregate([det(V.WARN), det(V.REVIEW)]) is V.REVIEW


def test_deterministic_block_beats_everything() -> None:
    assert aggregate([det(V.BLOCK), det(V.REVIEW), det(V.WARN), llm(V.PASS)]) is V.BLOCK


def test_llm_pass_cannot_relax_a_deterministic_block() -> None:
    """T32: QualityCritic が誤って PASS と言っても決定論 BLOCK が優先する。"""
    assert aggregate([det(V.BLOCK), llm(V.PASS)]) is V.BLOCK
    assert aggregate([llm(V.PASS), det(V.BLOCK)]) is V.BLOCK


def test_llm_pass_cannot_relax_a_deterministic_review() -> None:
    assert aggregate([det(V.REVIEW), llm(V.PASS)]) is V.REVIEW


def test_llm_can_only_make_it_stricter_and_block_is_capped_at_review() -> None:
    assert aggregate([det(V.PASS), llm(V.WARN)]) is V.WARN
    assert aggregate([det(V.PASS), llm(V.REVIEW)]) is V.REVIEW
    # LLM 単独の BLOCK は人の確認（REVIEW）止まり。自動で工程を落とす根拠にしない
    assert aggregate([det(V.PASS), llm(V.BLOCK)]) is V.REVIEW


def test_llm_only_pass_is_still_not_proof_of_pass() -> None:
    """決定論検査が 1 つも無いなら LLM の PASS だけでは PASS にならない。"""
    assert aggregate([llm(V.PASS)]) is V.REVIEW


def test_empty_checks_are_never_pass() -> None:
    assert aggregate([]) is V.REVIEW


@pytest.mark.parametrize("base", list(itertools.product(list(V), repeat=2)))
@pytest.mark.parametrize("extra", list(V))
def test_adding_an_llm_check_never_relaxes_the_result(
    base: tuple[QualityVerdict, QualityVerdict], extra: QualityVerdict
) -> None:
    """全組み合わせ: 決定論検査に LLM 検査を足しても結果は軽くならない（単調性）。"""
    order = [V.PASS, V.WARN, V.REVIEW, V.BLOCK]
    deterministic = [det(base[0]), det(base[1])]
    before = aggregate(deterministic)
    after = aggregate([*deterministic, llm(extra)])
    assert order.index(after) >= order.index(before)
