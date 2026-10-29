"""検査の記録・必須の集合の純粋な判定（ADR-0081 / INV-126, 127）。

テストの理由は docs/testing/v3-phase3-quality.md。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from contracts.quality import (
    MAX_REPORT_VALIDITY,
    QualityCheck,
    QualityGate,
    QualityReason,
    QualityVerdict,
)
from contracts.quality_records import CHECK_KEY_GATES, CheckKey, UsageFeature
from domain.quality.deterministic import QualityInputs, assess_for_auto_upload
from domain.quality.records import (
    CheckSetDraft,
    CheckSetInvalid,
    CheckSetProducer,
    CheckSetRecord,
    CheckSubject,
    ProducerContext,
    QualityRecordWriter,
    UsageDeclaration,
    new_check_set,
    recorded_checks_provider,
    required_keys,
    select_fresh_check_sets,
    select_usage,
    validate_check_set,
)

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
RULE = "deterministic-test"
SHA = "a" * 64
OTHER_SHA = "b" * 64
INPUT = "c" * 64
EP = "11111111-1111-1111-1111-111111111111"
SUBJECT = CheckSubject(EP, SHA, INPUT)


def _checks(
    key: CheckKey, verdict: QualityVerdict = QualityVerdict.PASS
) -> tuple[QualityCheck, ...]:
    ok = verdict is QualityVerdict.PASS
    return tuple(
        QualityCheck(
            gate=g,
            verdict=verdict,
            reason_code=QualityReason.OK if ok else QualityReason.AUDIO_DEFECT,
            detail="fixture",
            deterministic=True,
        )
        for g in sorted(CHECK_KEY_GATES[key], key=lambda g: g.value)
    )


def _record(
    key: CheckKey,
    *,
    subject: CheckSubject = SUBJECT,
    rule: str = RULE,
    generation: int = 1,
    checked_at: datetime = NOW,
    verdict: QualityVerdict = QualityVerdict.PASS,
) -> CheckSetRecord:
    draft = CheckSetDraft(key, _checks(key, verdict), "fixture-producer", "1")
    return new_check_set(
        record_id="r",
        subject=subject,
        draft=draft,
        rule_version=rule,
        generation=generation,
        checked_at=checked_at,
    )


def _declare(
    feature: UsageFeature, used: bool, subject: CheckSubject = SUBJECT
) -> UsageDeclaration:
    return UsageDeclaration(
        subject=subject,
        feature=feature,
        used=used,
        declared_by="render",
        declared_at=NOW,
        evidence_ref="evidence-1" if used else None,
    )


def _all_always() -> list[CheckSetRecord]:
    return [_record(CheckKey.TECHNICAL_QA), _record(CheckKey.DESCRIPTION_LINT)]


def _judge(records: list[CheckSetRecord], decls: list[UsageDeclaration], now: datetime = NOW):
    fresh = select_fresh_check_sets(records, SUBJECT, rule_version=RULE, now=now)
    usage = select_usage(decls, SUBJECT)
    return assess_for_auto_upload(
        QualityInputs(EP, SHA, INPUT),
        now=now,
        rule_version=RULE,
        providers=(recorded_checks_provider(fresh, usage),),
    )


NOT_USED = [_declare(UsageFeature.BGM, False), _declare(UsageFeature.MAP, False)]


# --- セットの形 ---------------------------------------------------------------------------


def test_a_set_must_cover_exactly_the_gates_of_its_key() -> None:
    only_music = tuple(c for c in _checks(CheckKey.BGM) if c.gate is QualityGate.MUSIC)
    with pytest.raises(CheckSetInvalid):
        validate_check_set(CheckKey.BGM, only_music)
    with pytest.raises(CheckSetInvalid):
        validate_check_set(CheckKey.MAP, _checks(CheckKey.BGM))
    with pytest.raises(CheckSetInvalid):
        validate_check_set(CheckKey.MAP, ())


def test_an_llm_check_cannot_be_recorded() -> None:
    llm = replace(_checks(CheckKey.MAP)[0], deterministic=False)
    with pytest.raises(CheckSetInvalid):
        validate_check_set(CheckKey.MAP, (llm,))


def test_expiry_is_fixed_to_the_maximum_validity() -> None:
    assert _record(CheckKey.MAP).expires_at - NOW == MAX_REPORT_VALIDITY


def test_naive_times_and_bad_hashes_cannot_be_recorded() -> None:
    with pytest.raises(CheckSetInvalid):
        _record(CheckKey.MAP, checked_at=datetime(2026, 10, 30, 12, 0))  # noqa: DTZ001
    with pytest.raises(CheckSetInvalid):
        CheckSubject(EP, "A" * 64, INPUT)
    with pytest.raises(CheckSetInvalid):
        CheckSubject(EP, SHA, "short")


def test_usage_declaration_requires_evidence_iff_used() -> None:
    with pytest.raises(CheckSetInvalid):
        UsageDeclaration(SUBJECT, UsageFeature.BGM, True, "render", NOW, None)
    with pytest.raises(CheckSetInvalid):
        UsageDeclaration(SUBJECT, UsageFeature.BGM, False, "render", NOW, "evidence-1")


# --- 再利用できない（INV-126）-------------------------------------------------------------


def _fresh(records: list[CheckSetRecord], now: datetime = NOW):
    return select_fresh_check_sets(records, SUBJECT, rule_version=RULE, now=now)


def test_a_record_for_another_final_video_hash_is_not_counted() -> None:
    other = _record(CheckKey.TECHNICAL_QA, subject=CheckSubject(EP, OTHER_SHA, INPUT))
    result = _fresh([other])
    assert CheckKey.TECHNICAL_QA not in result.fresh
    assert result.rejected[CheckKey.TECHNICAL_QA] is QualityReason.REPORT_FINAL_VIDEO_MISMATCH


def test_a_record_for_another_episode_is_not_counted() -> None:
    other = _record(CheckKey.TECHNICAL_QA, subject=CheckSubject("other-episode", SHA, INPUT))
    assert CheckKey.TECHNICAL_QA not in _fresh([other]).fresh


def test_a_record_for_another_input_hash_is_not_counted() -> None:
    other = _record(CheckKey.TECHNICAL_QA, subject=CheckSubject(EP, SHA, "d" * 64))
    result = _fresh([other])
    assert result.rejected[CheckKey.TECHNICAL_QA] is QualityReason.REPORT_INPUT_HASH_MISMATCH


def test_a_record_for_an_older_rule_version_is_not_counted() -> None:
    result = _fresh([_record(CheckKey.TECHNICAL_QA, rule="deterministic-old")])
    assert result.rejected[CheckKey.TECHNICAL_QA] is QualityReason.REPORT_RULE_VERSION_MISMATCH


def test_an_expired_record_is_not_counted_and_the_boundary_is_exclusive() -> None:
    record = _record(CheckKey.TECHNICAL_QA)
    assert CheckKey.TECHNICAL_QA in _fresh([record], NOW + MAX_REPORT_VALIDITY - timedelta(1)).fresh
    expired = _fresh([record], NOW + MAX_REPORT_VALIDITY)
    assert expired.rejected[CheckKey.TECHNICAL_QA] is QualityReason.REPORT_EXPIRED


def test_a_record_from_the_future_is_not_counted() -> None:
    future = _record(CheckKey.TECHNICAL_QA, checked_at=NOW + timedelta(hours=1))
    assert _fresh([future]).rejected[CheckKey.TECHNICAL_QA] is QualityReason.REPORT_FROM_THE_FUTURE


def test_a_record_with_an_incoherent_validity_is_not_counted() -> None:
    record = replace(_record(CheckKey.TECHNICAL_QA), expires_at=NOW + timedelta(days=30))
    assert (
        _fresh([record]).rejected[CheckKey.TECHNICAL_QA] is QualityReason.REPORT_FRESHNESS_INVALID
    )


def test_the_newest_generation_wins_and_a_stale_newest_hides_the_older_one() -> None:
    old_pass = _record(CheckKey.TECHNICAL_QA, generation=1)
    new_block = _record(CheckKey.TECHNICAL_QA, generation=2, verdict=QualityVerdict.BLOCK)
    assert _fresh([old_pass, new_block]).fresh[CheckKey.TECHNICAL_QA].generation == 2
    assert _fresh([new_block, old_pass]).fresh[CheckKey.TECHNICAL_QA].generation == 2


def test_a_declaration_for_another_hash_is_not_used() -> None:
    other = _declare(UsageFeature.BGM, False, CheckSubject(EP, OTHER_SHA, INPUT))
    assert select_usage([other], SUBJECT) == {}


# --- 必須の集合（INV-127）----------------------------------------------------------------


def test_everything_present_and_fresh_with_declared_non_use_is_allowed() -> None:
    assessment = _judge(_all_always(), NOT_USED)
    assert assessment.decision.allowed
    assert assessment.verdict is QualityVerdict.PASS


def test_no_records_at_all_is_refused_as_review() -> None:
    assessment = _judge([], [])
    assert not assessment.decision.allowed
    assert assessment.verdict is QualityVerdict.REVIEW


@pytest.mark.parametrize("missing", [CheckKey.TECHNICAL_QA, CheckKey.DESCRIPTION_LINT])
def test_each_always_required_check_is_required(missing: CheckKey) -> None:
    records = [r for r in _all_always() if r.key is not missing]
    assessment = _judge(records, NOT_USED)
    assert not assessment.decision.allowed
    assert assessment.verdict is QualityVerdict.REVIEW


@pytest.mark.parametrize("feature", list(UsageFeature))
def test_an_undeclared_feature_is_refused_not_treated_as_unused(feature: UsageFeature) -> None:
    decls = [d for d in NOT_USED if d.feature is not feature]
    assessment = _judge(_all_always(), decls)
    assert not assessment.decision.allowed
    assert assessment.verdict is QualityVerdict.REVIEW


@pytest.mark.parametrize(
    ("feature", "key"), [(UsageFeature.BGM, CheckKey.BGM), (UsageFeature.MAP, CheckKey.MAP)]
)
def test_a_used_feature_requires_its_check_set(feature: UsageFeature, key: CheckKey) -> None:
    decls = [d for d in NOT_USED if d.feature is not feature] + [_declare(feature, True)]
    without = _judge(_all_always(), decls)
    assert not without.decision.allowed
    with_set = _judge([*_all_always(), _record(key)], decls)
    assert with_set.decision.allowed


def test_a_used_feature_with_a_stale_set_is_refused() -> None:
    decls = [_declare(UsageFeature.BGM, True), _declare(UsageFeature.MAP, False)]
    stale = _record(CheckKey.BGM, subject=CheckSubject(EP, OTHER_SHA, INPUT))
    assert not _judge([*_all_always(), stale], decls).decision.allowed


def test_a_recorded_block_is_refused_even_when_everything_else_passes() -> None:
    records = [_record(CheckKey.TECHNICAL_QA, verdict=QualityVerdict.BLOCK), *_all_always()[1:]]
    assessment = _judge(records, NOT_USED)
    assert assessment.verdict is QualityVerdict.BLOCK
    assert not assessment.decision.allowed


def test_required_keys_follow_the_declarations() -> None:
    usage = select_usage(
        [_declare(UsageFeature.BGM, True), _declare(UsageFeature.MAP, False)], SUBJECT
    )
    assert required_keys(usage) == (CheckKey.TECHNICAL_QA, CheckKey.DESCRIPTION_LINT, CheckKey.BGM)


def test_the_provider_does_not_raise_on_empty_input() -> None:
    provider = recorded_checks_provider(_fresh([]), {})
    assert provider(QualityInputs(EP, SHA, INPUT))


# --- 他の枝が差し込む口（fake での接続確認）----------------------------------------------


class _FakeProducer:
    key = CheckKey.BGM

    async def produce(self, context: ProducerContext, *, now: datetime) -> CheckSetDraft | None:
        return CheckSetDraft(CheckKey.BGM, _checks(CheckKey.BGM), "fake-bgm", "1", "evidence-1")


class _FakeWriter:
    def __init__(self) -> None:
        self.sets: list[CheckSetRecord] = []
        self.declared: list[UsageDeclaration] = []

    async def write_check_set(self, subject, draft, *, checked_at, rule_version):  # type: ignore[no-untyped-def]
        record = new_check_set(
            record_id=str(len(self.sets)),
            subject=subject,
            draft=draft,
            rule_version=rule_version,
            generation=len(self.sets) + 1,
            checked_at=checked_at,
        )
        self.sets.append(record)
        return record

    async def declare_usage(  # type: ignore[no-untyped-def]
        self, subject, feature, *, used, declared_by, declared_at, evidence_ref=None
    ):
        decl = UsageDeclaration(subject, feature, used, declared_by, declared_at, evidence_ref)
        self.declared.append(decl)
        return decl


@pytest.mark.asyncio
async def test_a_fake_producer_and_writer_satisfy_the_protocols_and_flow_to_an_allow() -> None:
    producer: CheckSetProducer = _FakeProducer()
    writer: QualityRecordWriter = _FakeWriter()
    draft = await producer.produce(None, now=NOW)  # type: ignore[arg-type]
    assert draft is not None
    await writer.write_check_set(SUBJECT, draft, checked_at=NOW, rule_version=RULE)
    await writer.declare_usage(
        SUBJECT,
        UsageFeature.BGM,
        used=True,
        declared_by="render",
        declared_at=NOW,
        evidence_ref="evidence-1",
    )
    await writer.declare_usage(
        SUBJECT, UsageFeature.MAP, used=False, declared_by="render", declared_at=NOW
    )
    assert isinstance(writer, _FakeWriter)
    assessment = _judge([*_all_always(), *writer.sets], writer.declared)
    assert assessment.decision.allowed


@pytest.mark.asyncio
async def test_a_producer_that_returns_nothing_leaves_the_set_missing() -> None:
    class _Silent:
        key = CheckKey.MAP

        async def produce(self, context, *, now):  # type: ignore[no-untyped-def]
            return None

    assert await _Silent().produce(None, now=NOW) is None
    decls = [_declare(UsageFeature.BGM, False), _declare(UsageFeature.MAP, True)]
    assert not _judge(_all_always(), decls).decision.allowed
