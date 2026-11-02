"""WARN は決定論の検査が WARN と判定したものだけで、BLOCK・REVIEW を緩める経路は無い（INV-157）。

表は網羅（総当たり）で、乱数を使わない。理由は docs/testing/bgm-auto-quality-rationale.md。
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime

import pytest

from contracts.quality import (
    QUALITY_RULE_VERSION,
    QualityCheck,
    QualityGate,
    QualityReason,
    QualityVerdict,
)
from contracts.verdict import VERDICT_SEVERITY
from domain.quality.aggregate import aggregate
from domain.quality.inspection import refine_hold_reason
from domain.quality.upload_decision import UploadDecision, evaluate_for_upload

V = tuple(QualityVerdict)
_REASON = {
    QualityVerdict.PASS: QualityReason.OK,
    QualityVerdict.WARN: QualityReason.MINOR_WORDING,
    QualityVerdict.REVIEW: QualityReason.LICENSE_UNVERIFIED,
    QualityVerdict.BLOCK: QualityReason.AUDIO_DEFECT,
}


def _check(verdict: QualityVerdict, deterministic: bool, gate=QualityGate.AUDIO) -> QualityCheck:
    return QualityCheck(gate, verdict, _REASON[verdict], "fixed detail", deterministic)


def _sev(v: QualityVerdict) -> int:
    return VERDICT_SEVERITY[v]


#: (verdict, deterministic) の全組み合わせ 8 通りから 0〜3 個を選ぶ（順序も含めて総当たり）
_ITEMS = [(v, d) for v in V for d in (True, False)]
_CASES = [combo for n in range(0, 4) for combo in itertools.product(_ITEMS, repeat=n)]


def _case_id(combo) -> str:  # type: ignore[no-untyped-def]
    return "|".join(f"{v.value}{'D' if d else 'L'}" for v, d in combo) or "empty"


@pytest.mark.parametrize("combo", _CASES, ids=_case_id)
def test_aggregate_can_only_be_as_strict_as_the_strictest_deterministic_check(combo) -> None:
    checks = [_check(v, d) for v, d in combo]
    result = aggregate(checks)
    deterministic = [v for v, d in combo if d]
    # 決定論の検査の最も厳しい判定より緩くはならない（LLM の PASS/WARN は何も緩めない）
    if deterministic:
        assert _sev(result) >= max(_sev(v) for v in deterministic)
    else:
        # 決定論の根拠が無ければ PASS/WARN にならない
        assert _sev(result) >= _sev(QualityVerdict.REVIEW)
    # LLM の検査は BLOCK を作れない（上限は REVIEW）
    if not any(d and v is QualityVerdict.BLOCK for v, d in combo):
        assert result is not QualityVerdict.BLOCK
    # 全体として、どの検査より緩い結果にならない（REVIEW/BLOCK を含むなら PASS/WARN にならない）
    if any(v in (QualityVerdict.REVIEW, QualityVerdict.BLOCK) for v, _ in combo):
        assert result in (QualityVerdict.REVIEW, QualityVerdict.BLOCK)


def test_the_cap_of_llm_checks_is_review() -> None:
    assert aggregate([_check(QualityVerdict.PASS, True), _check(QualityVerdict.BLOCK, False)]) is (
        QualityVerdict.REVIEW
    )


NOW = datetime(2026, 11, 2, tzinfo=UTC)


@pytest.mark.parametrize("verdict", V)
def test_only_pass_and_warn_reports_are_ever_allowed(verdict: QualityVerdict) -> None:
    from contracts.quality import MAX_REPORT_VALIDITY, QualityReport

    checks = (_check(verdict, True),)
    report = QualityReport(
        episode_id="ep-1",
        final_video_sha256="a" * 64,
        input_hash="b" * 64,
        generated_at=NOW,
        generator_version="g",
        reviewer_version="r",
        rule_version=QUALITY_RULE_VERSION,
        expires_at=NOW + MAX_REPORT_VALIDITY,
        verdict=aggregate(checks),
        checks=checks,
        model=None,
    )
    decision = evaluate_for_upload(
        report,
        current_final_video_sha256="a" * 64,
        current_input_hash="b" * 64,
        current_rule_version=QUALITY_RULE_VERSION,
        now=NOW,
    )
    assert decision.allowed is (verdict in (QualityVerdict.PASS, QualityVerdict.WARN))


@pytest.mark.parametrize("reason", list(QualityReason))
@pytest.mark.parametrize("verdict", [QualityVerdict.REVIEW, QualityVerdict.BLOCK])
def test_refining_a_hold_reason_never_produces_an_allowance(
    reason: QualityReason, verdict: QualityVerdict
) -> None:
    holds = [_check(verdict, True)]
    for decision in (UploadDecision(False, reason), UploadDecision(True, reason)):
        refined = refine_hold_reason(decision, holds)
        assert refined.allowed is decision.allowed  # 許可は増えも減りもしない


# --- WARN を作る場所は決まっている（新しい「軽微」を黙って足さない） --------------------------

#: ``Verdict.WARN`` / ``GeoVerdict.WARN`` を**書いてよい**ファイル。生産者 = 検査自身が軽微と
#: 判定するもの（地図の許容距離を少し超えたとき。``classify_distance`` と関連語の有無）。
#: 消費者 = WARN を投稿可として読むだけのもの。ここに足すときは INV-157 の根拠を ADR に書く。
WARN_PRODUCERS = frozenset({"domain/geo/tolerance.py", "domain/geo/verify.py"})
WARN_CONSUMERS = frozenset(
    {
        "contracts/verdict.py",
        "domain/quality/upload_decision.py",
        "domain/quality/outcome.py",
        "domain/quality/hold_rate.py",
    }
)


def test_warn_is_produced_only_by_the_audited_deterministic_checks() -> None:
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[2]
    pattern = re.compile(r"\b(?:Quality|Geo)?Verdict\.WARN\b")
    found: set[str] = set()
    for top in ("domain", "infrastructure", "workers", "apps", "contracts"):
        for path in (root / top).rglob("*.py"):
            if pattern.search(path.read_text()):
                found.add(str(path.relative_to(root)))
    assert found <= WARN_PRODUCERS | WARN_CONSUMERS, sorted(found - WARN_PRODUCERS - WARN_CONSUMERS)
    # 監査表が実態とずれていない（古い行を残さない）
    assert found >= WARN_PRODUCERS
