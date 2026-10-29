"""決定論の検査だけで QualityReport を組み立て、投稿可否を判定する（ADR-0076 §3 / §4）。純粋。

入力は gate が新しい保存先なしで読めるものだけ（final_video の sha256・入力 hash、あれば技術 QA、
あれば説明欄）。検査は ``CheckProvider``（``QualityInputs -> checks``）の列で、他の決定論の検査
（地図・BGM 等）は gate を触らずに列へ足せる。入力が無い provider は何も返さない（「該当なし」を
PASS にしない）。決定論の検査が 1 つも無ければ集計は REVIEW（ADR-0073 §2 / INV-101）。
AI は使わない（model=None）。

時計・規則版は引数で注入される（実時計を読まない / INV-107）。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from contracts.quality import (
    MAX_REPORT_VALIDITY,
    QualityCheck,
    QualityGate,
    QualityReason,
    QualityReport,
    QualityReportFailure,
    QualityVerdict,
    validate_quality_report,
)
from contracts.render import TechnicalQaReport
from contracts.upload import find_internal_ids
from domain.quality.aggregate import aggregate
from domain.quality.freshness import check_freshness
from domain.quality.upload_decision import UploadDecision, evaluate_for_upload

GENERATOR_VERSION = "deterministic-builder-1"
REVIEWER_VERSION = "none"


@dataclass(frozen=True, slots=True)
class QualityInputs:
    """gate 時点で読める入力。``None`` は「読めなかった」（検査は黙って PASS にしない）。"""

    episode_id: str
    final_video_sha256: str
    input_hash: str
    technical_qa: TechnicalQaReport | None = None
    description: str | None = None


CheckProvider = Callable[[QualityInputs], Sequence[QualityCheck]]


def technical_qa_provider(inputs: QualityInputs) -> Sequence[QualityCheck]:
    """保存済みの技術 QA（``TechnicalQaReport``。合格時のみ保存される）。無ければ検査なし。"""
    if inputs.technical_qa is None:
        return ()
    ok = inputs.technical_qa.passed and all(c.passed for c in inputs.technical_qa.checks)
    return (
        QualityCheck(
            gate=QualityGate.VISUAL,
            verdict=QualityVerdict.PASS if ok else QualityVerdict.BLOCK,
            reason_code=QualityReason.OK if ok else QualityReason.VISUAL_DEFECT,
            detail="technical qa recorded" if ok else "technical qa failed",
            deterministic=True,
        ),
    )


def description_lint_provider(inputs: QualityInputs) -> Sequence[QualityCheck]:
    """説明欄に内部 ID が無いこと（INV-90 の lint）。説明欄が読めなければ検査なし。"""
    if inputs.description is None:
        return ()
    clean = not find_internal_ids(inputs.description)
    return (
        QualityCheck(
            gate=QualityGate.METADATA,
            verdict=QualityVerdict.PASS if clean else QualityVerdict.BLOCK,
            reason_code=QualityReason.OK if clean else QualityReason.METADATA_LINT_FAILED,
            detail="description lint clean" if clean else "description lint failed",
            deterministic=True,
        ),
    )


#: 既定の provider 列。他ブランチの決定論の検査（地図・BGM）はここへ足す（gate 本体は触らない）。
DEFAULT_PROVIDERS: tuple[CheckProvider, ...] = (technical_qa_provider, description_lint_provider)


def build_deterministic_report(
    inputs: QualityInputs,
    *,
    now: datetime,
    rule_version: str,
    providers: Sequence[CheckProvider] = DEFAULT_PROVIDERS,
) -> QualityReport:
    """provider の検査を集めて報告を作る。provider の例外は伝播する（呼び出し側が拒否に倒す）。"""
    checks: list[QualityCheck] = []
    for provider in providers:
        checks.extend(provider(inputs))
    return QualityReport(
        episode_id=inputs.episode_id,
        final_video_sha256=inputs.final_video_sha256,
        input_hash=inputs.input_hash,
        generated_at=now,
        generator_version=GENERATOR_VERSION,
        reviewer_version=REVIEWER_VERSION,
        rule_version=rule_version,
        expires_at=now + MAX_REPORT_VALIDITY,
        verdict=aggregate(checks),
        checks=tuple(checks),
        model=None,
    )


@dataclass(frozen=True, slots=True)
class QualityAssessment:
    """判定と、その根拠になった verdict（作れなかったときは ``None``）。"""

    decision: UploadDecision
    verdict: QualityVerdict | None
    #: 手動経路で REVIEW を人が承認した（自動経路では常に False）
    review_approved: bool = False


def _refused(reason: QualityReason, verdict: QualityVerdict | None = None) -> QualityAssessment:
    return QualityAssessment(UploadDecision(False, reason), verdict)


def _build_and_evaluate(
    inputs: QualityInputs,
    *,
    now: datetime,
    rule_version: str,
    providers: Sequence[CheckProvider],
    allow_review: bool,
) -> QualityAssessment:
    try:
        report = build_deterministic_report(
            inputs, now=now, rule_version=rule_version, providers=providers
        )
    except Exception:  # noqa: BLE001 - fail-closed: 作れなかったものは通さない
        return QualityAssessment(
            evaluate_for_upload(
                QualityReportFailure(),
                current_final_video_sha256=inputs.final_video_sha256,
                current_input_hash=inputs.input_hash,
                current_rule_version=rule_version,
                now=now,
            ),
            None,
        )
    decision = evaluate_for_upload(
        report,
        current_final_video_sha256=inputs.final_video_sha256,
        current_input_hash=inputs.input_hash,
        current_rule_version=rule_version,
        now=now,
    )
    if decision.allowed:
        return QualityAssessment(decision, report.verdict)
    if allow_review and _is_plain_review(report, inputs, rule_version, now, decision):
        return QualityAssessment(
            UploadDecision(True, QualityReason.REPORT_NEEDS_REVIEW), report.verdict, True
        )
    return QualityAssessment(decision, report.verdict)


def _is_plain_review(
    report: QualityReport,
    inputs: QualityInputs,
    rule_version: str,
    now: datetime,
    decision: UploadDecision,
) -> bool:
    """REVIEW だけが理由の拒否か（鮮度・形・BLOCK などの別の理由が混じっていない）。"""
    if report.verdict is not QualityVerdict.REVIEW:
        return False
    if decision.reason_code is QualityReason.REPORT_NEEDS_REVIEW:
        return True
    if decision.reason_code is not QualityReason.REPORT_EMPTY_CHECKS:
        return False
    # 検査が 1 つも無い報告は evaluate が鮮度の前に拒否する。人の承認に使うなら鮮度は別に確かめる
    try:
        validate_quality_report(report)
    except Exception:  # noqa: BLE001
        return False
    return (
        check_freshness(
            report,
            current_final_video_sha256=inputs.final_video_sha256,
            current_input_hash=inputs.input_hash,
            current_rule_version=rule_version,
            now=now,
        )
        is None
    )


def assess_for_auto_upload(
    inputs: QualityInputs,
    *,
    now: datetime,
    rule_version: str,
    providers: Sequence[CheckProvider] = DEFAULT_PROVIDERS,
) -> QualityAssessment:
    """自動経路: PASS / WARN だけ許可。REVIEW・BLOCK・作成失敗・評価エラーは拒否。"""
    try:
        return _build_and_evaluate(
            inputs, now=now, rule_version=rule_version, providers=providers, allow_review=False
        )
    except Exception:  # noqa: BLE001 - 想定外は PASS に倒さない
        return _refused(QualityReason.REPORT_EVALUATION_ERROR)


def assess_for_manual_upload(
    inputs: QualityInputs,
    *,
    now: datetime,
    rule_version: str,
    providers: Sequence[CheckProvider] = DEFAULT_PROVIDERS,
) -> QualityAssessment:
    """手動経路: BLOCK と作成失敗は拒否。REVIEW は人の承認として許可する（ADR-0076 §4）。"""
    try:
        return _build_and_evaluate(
            inputs, now=now, rule_version=rule_version, providers=providers, allow_review=True
        )
    except Exception:  # noqa: BLE001
        return _refused(QualityReason.REPORT_EVALUATION_ERROR)
