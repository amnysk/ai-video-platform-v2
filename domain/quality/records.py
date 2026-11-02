"""検査の記録・使用宣言・必須の集合の判定（ADR-0081）。純粋: I/O・時計を持たない。

記録は「この対象 hash の完成動画について、この規則版で、この時刻に、この producer が出した」という
主張。今の対象 hash・入力 hash・規則版・時刻に結び付かない記録は**無いものとして扱う**（INV-126）。
必須の検査が欠けるときは REVIEW の決定論の検査を足す（PASS にならない / INV-127）。
BGM・地図などの他の枝が実装する producer の差し込み口（``CheckSetProducer`` /
``QualityRecordWriter``）もここに置く。
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from contracts.artifacts import ScriptArtifact
from contracts.quality import (
    CLOCK_SKEW_TOLERANCE,
    MAX_REPORT_VALIDITY,
    QualityCheck,
    QualityGate,
    QualityReason,
    QualityVerdict,
)
from contracts.quality_records import (
    ALWAYS_REQUIRED_KEYS,
    CHECK_KEY_GATES,
    DETAIL_DECLARED_NOT_USED,
    DETAIL_INSPECTION_UNAVAILABLE,
    DETAIL_REQUIRED_MISSING,
    DETAIL_USAGE_MISSING,
    FEATURE_CHECK_KEY,
    CheckKey,
    UsageFeature,
)
from contracts.render import FinalVideoArtifact
from contracts.untrusted import is_safe_id
from domain.quality.aggregate import aggregate
from domain.quality.deterministic import CheckProvider, QualityInputs

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class CheckSetInvalid(ValueError):
    """検査セット / 宣言として成り立たない。記録しない・読んでも使わない。"""


def _aware(value: object) -> bool:
    return isinstance(value, datetime) and value.utcoffset() is not None


@dataclass(frozen=True, slots=True)
class CheckSubject:
    """記録の対象。``final_video_sha256`` は現行 final_video の ``artifact_metadata.sha256``。

    ADR-0081 §1。
    """

    episode_id: str
    final_video_sha256: str
    input_hash: str

    def __post_init__(self) -> None:
        if not self.episode_id.strip():
            raise CheckSetInvalid("episode_id must not be empty")
        if not _SHA256.fullmatch(self.final_video_sha256):
            raise CheckSetInvalid("final_video_sha256 must be a lowercase sha256 hex digest")
        if not _SHA256.fullmatch(self.input_hash):
            raise CheckSetInvalid("input_hash must be a lowercase sha256 hex digest")


def validate_check_set(key: CheckKey, checks: Sequence[QualityCheck]) -> None:
    """セットが対応する gate をすべて含み、対応外を含まず、決定論だけであること。"""
    if not checks:
        raise CheckSetInvalid("a check set must not be empty")
    expected = CHECK_KEY_GATES[key]
    gates = {c.gate for c in checks}
    if gates != expected:
        raise CheckSetInvalid(
            f"check set {key.value} must cover exactly gates {sorted(g.value for g in expected)}"
        )
    if not all(c.deterministic for c in checks):
        raise CheckSetInvalid("a recorded check must be deterministic")


@dataclass(frozen=True, slots=True)
class CheckSetDraft:
    """producer が返す未保存のセット。時刻・世代・id は writer が付ける。"""

    key: CheckKey
    checks: tuple[QualityCheck, ...]
    producer: str
    producer_version: str
    evidence_ref: str | None = None

    def __post_init__(self) -> None:
        validate_check_set(self.key, self.checks)
        if not is_safe_id(self.producer) or not is_safe_id(self.producer_version):
            raise CheckSetInvalid("producer and producer_version must match the safe id pattern")
        if self.evidence_ref is not None and not is_safe_id(self.evidence_ref):
            raise CheckSetInvalid("evidence_ref must match the safe id pattern")


@dataclass(frozen=True, slots=True)
class CheckSetRecord:
    """保存された検査セット（追記専用）。``generation`` は同じ key の再検査で 1 ずつ上がる。"""

    id: str
    subject: CheckSubject
    rule_version: str
    key: CheckKey
    generation: int
    checks: tuple[QualityCheck, ...]
    producer: str
    producer_version: str
    checked_at: datetime
    expires_at: datetime
    evidence_ref: str | None = None

    def __post_init__(self) -> None:
        validate_check_set(self.key, self.checks)
        if self.generation < 1:
            raise CheckSetInvalid("generation must be >= 1")

    @property
    def verdict(self) -> QualityVerdict:
        return aggregate(self.checks)


def new_check_set(
    *,
    record_id: str,
    subject: CheckSubject,
    draft: CheckSetDraft,
    rule_version: str,
    generation: int,
    checked_at: datetime,
) -> CheckSetRecord:
    """書き込み用のレコードを作る。期限は ``checked_at + MAX_REPORT_VALIDITY`` に固定する。"""
    if not _aware(checked_at):
        raise CheckSetInvalid("checked_at must be timezone-aware")
    if not rule_version.strip():
        raise CheckSetInvalid("rule_version must not be empty")
    return CheckSetRecord(
        id=record_id,
        subject=subject,
        rule_version=rule_version,
        key=draft.key,
        generation=generation,
        checks=draft.checks,
        producer=draft.producer,
        producer_version=draft.producer_version,
        checked_at=checked_at,
        expires_at=checked_at + MAX_REPORT_VALIDITY,
        evidence_ref=draft.evidence_ref,
    )


@dataclass(frozen=True, slots=True)
class UsageDeclaration:
    """render が書く「この完成動画で機能が使われたか」の宣言（ADR-0081 §3）。"""

    subject: CheckSubject
    feature: UsageFeature
    used: bool
    declared_by: str
    declared_at: datetime
    evidence_ref: str | None = None

    def __post_init__(self) -> None:
        if not is_safe_id(self.declared_by):
            raise CheckSetInvalid("declared_by must match the safe id pattern")
        if not _aware(self.declared_at):
            raise CheckSetInvalid("declared_at must be timezone-aware")
        if self.used and self.evidence_ref is None:
            raise CheckSetInvalid("a 'used' declaration must carry an evidence_ref")
        if not self.used and self.evidence_ref is not None:
            raise CheckSetInvalid("a 'not used' declaration must not carry an evidence_ref")
        if self.evidence_ref is not None and not is_safe_id(self.evidence_ref):
            raise CheckSetInvalid("evidence_ref must match the safe id pattern")


@dataclass(frozen=True, slots=True)
class FreshCheckSets:
    """今の対象に結び付く有効なセット（key ごとに最新世代 1 つ）と、数えなかった理由。"""

    fresh: Mapping[CheckKey, CheckSetRecord]
    rejected: Mapping[CheckKey, QualityReason] = field(default_factory=dict)


def _freshness_reason(record: CheckSetRecord, now: datetime) -> QualityReason | None:
    if not _aware(record.checked_at) or not _aware(record.expires_at) or not _aware(now):
        return QualityReason.REPORT_TIMESTAMP_INVALID
    if (
        record.expires_at <= record.checked_at
        or record.expires_at - record.checked_at > MAX_REPORT_VALIDITY
    ):
        return QualityReason.REPORT_FRESHNESS_INVALID
    if record.checked_at > now + CLOCK_SKEW_TOLERANCE:
        return QualityReason.REPORT_FROM_THE_FUTURE
    if now >= record.expires_at:
        return QualityReason.REPORT_EXPIRED
    return None


def select_fresh_check_sets(
    records: Iterable[CheckSetRecord],
    subject: CheckSubject,
    *,
    rule_version: str,
    now: datetime,
) -> FreshCheckSets:
    """取得した行から、今の対象・入力・規則版・時刻に結び付くものだけを選ぶ（INV-126）。

    SQL の絞り込みとは独立に**必ず**ここで検証する。別 hash・別入力・別規則版・期限切れの行は
    「無い」と同じ扱いで、数えなかった理由だけを ``rejected`` に残す。
    """
    by_key: dict[CheckKey, list[CheckSetRecord]] = defaultdict(list)
    rejected: dict[CheckKey, QualityReason] = {}
    for record in records:
        if (
            record.subject.episode_id != subject.episode_id
            or record.subject.final_video_sha256 != subject.final_video_sha256
        ):
            rejected[record.key] = QualityReason.REPORT_FINAL_VIDEO_MISMATCH
        elif record.subject.input_hash != subject.input_hash:
            rejected[record.key] = QualityReason.REPORT_INPUT_HASH_MISMATCH
        elif not rule_version or record.rule_version != rule_version:
            rejected[record.key] = QualityReason.REPORT_RULE_VERSION_MISMATCH
        else:
            by_key[record.key].append(record)
    fresh: dict[CheckKey, CheckSetRecord] = {}
    for key, candidates in by_key.items():
        newest = max(candidates, key=lambda r: r.generation)
        reason = _freshness_reason(newest, now)
        if reason is None:
            fresh[key] = newest
            rejected.pop(key, None)
        else:
            rejected[key] = reason
    return FreshCheckSets(fresh=fresh, rejected=rejected)


def select_usage(
    declarations: Iterable[UsageDeclaration], subject: CheckSubject
) -> dict[UsageFeature, UsageDeclaration]:
    """今の対象（episode・sha・input_hash）に結び付く宣言だけ。他の hash の宣言は無いものとする。"""
    out: dict[UsageFeature, UsageDeclaration] = {}
    for d in declarations:
        if d.subject == subject:
            out.setdefault(d.feature, d)
    return out


def required_keys(usage: Mapping[UsageFeature, UsageDeclaration]) -> tuple[CheckKey, ...]:
    """今の宣言から求まる必須のセット。宣言が無い機能の検査は、宣言の欠落として別に報告する。"""
    keys = list(ALWAYS_REQUIRED_KEYS)
    for feature, key in FEATURE_CHECK_KEY.items():
        declaration = usage.get(feature)
        if declaration is not None and declaration.used:
            keys.append(key)
    return tuple(keys)


def _missing(
    gates: Iterable[QualityGate], reason: QualityReason, detail: str
) -> list[QualityCheck]:
    return [
        QualityCheck(
            gate=gate,
            verdict=QualityVerdict.REVIEW,
            reason_code=reason,
            detail=detail,
            deterministic=True,
        )
        for gate in sorted(gates, key=lambda g: g.value)
    ]


def recorded_checks_provider(
    fresh: FreshCheckSets,
    usage: Mapping[UsageFeature, UsageDeclaration],
    *,
    unavailable: frozenset[CheckKey] = frozenset(),
) -> CheckProvider:
    """有効な記録 + 欠落の REVIEW + 宣言された不使用の PASS を、1 つの ``CheckProvider`` にする。

    ``unavailable`` は検査系の一時障害で作れなかった必須のセット（ADR-0087 §8）。欠落の REVIEW の
    理由だけを ``INSPECTION_UNAVAILABLE`` にする（判定は同じ REVIEW。PASS にならない）。

    返す provider は ``QualityInputs`` を使わない（記録が全てを持つ）。例外を投げない。
    """

    def provider(_: QualityInputs) -> Sequence[QualityCheck]:
        checks: list[QualityCheck] = []
        for key in CheckKey:
            record = fresh.fresh.get(key)
            if record is not None:
                checks.extend(record.checks)
        for key in required_keys(usage):
            if key not in fresh.fresh:
                lost = key in unavailable
                checks.extend(
                    _missing(
                        CHECK_KEY_GATES[key],
                        QualityReason.INSPECTION_UNAVAILABLE
                        if lost
                        else QualityReason.REQUIRED_CHECK_MISSING,
                        DETAIL_INSPECTION_UNAVAILABLE if lost else DETAIL_REQUIRED_MISSING,
                    )
                )
        for feature, key in FEATURE_CHECK_KEY.items():
            declaration = usage.get(feature)
            if declaration is None:
                checks.extend(
                    _missing(
                        CHECK_KEY_GATES[key],
                        QualityReason.USAGE_DECLARATION_MISSING,
                        DETAIL_USAGE_MISSING,
                    )
                )
            elif not declaration.used:
                checks.extend(
                    QualityCheck(
                        gate=gate,
                        verdict=QualityVerdict.PASS,
                        reason_code=QualityReason.OK,
                        detail=DETAIL_DECLARED_NOT_USED,
                        deterministic=True,
                    )
                    for gate in sorted(CHECK_KEY_GATES[key], key=lambda g: g.value)
                )
        return checks

    return provider


class ProducerContext(Protocol):
    """gate が読み込み済みの入力。producer が object storage や DB を再実装しないための口。"""

    @property
    def subject(self) -> CheckSubject: ...

    async def final_video(self) -> FinalVideoArtifact:
        """sha256 が ``subject.final_video_sha256`` と一致することを確認済みの完成動画 JSON。"""
        ...

    async def script(self) -> ScriptArtifact:
        """final_video が指す台本（sha256 照合済み）。"""
        ...


class CheckSetProducer(Protocol):
    """gate 時点に検査セットを作る口。BGM・地図の枝が純関数を包んで差し込む（ADR-0081 §7）。

    ``None`` を返す・例外を投げる = 記録しない（必須なら REVIEW。PASS に倒れない）。
    """

    key: CheckKey

    async def produce(self, context: ProducerContext, *, now: datetime) -> CheckSetDraft | None: ...


class QualityRecordWriter(Protocol):
    """記録の書き込み口。render（宣言・直接書き込み）と gate が使う。追記専用。"""

    async def write_check_set(
        self,
        subject: CheckSubject,
        draft: CheckSetDraft,
        *,
        checked_at: datetime,
        rule_version: str,
    ) -> CheckSetRecord: ...

    async def declare_usage(
        self,
        subject: CheckSubject,
        feature: UsageFeature,
        *,
        used: bool,
        declared_by: str,
        declared_at: datetime,
        evidence_ref: str | None = None,
    ) -> UsageDeclaration: ...
