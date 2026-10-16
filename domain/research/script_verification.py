"""台本の文面を Evidence と照合する規則（ADR-0038 §4）。純粋・決定的（INV-6）。

入力は台本の文面（``ScriptUnitInput``）と Evidence の成果物。出力は
``research_script_verification`` の payload（**契約を通したもの**。契約は「passed は Evidence が
completed で、全 check が ok、未登録が空のときだけ」を強制するので、ここが甘くしても作れない）。

1. 文面を Evidence の claim と**語・数値の重なり**で対応付ける（書き手の claim id は受け取らない）
2. 対応した claim の ``usable_expression`` と比べて、量化子・因果・最上級・数値・確度が強すぎないか
3. 主張らしい徴候（年代・数量・最上級/全称・因果・引用発言）のある文で、どの claim にも対応しない
   ものを「未登録の主張」とする（決定的な網。``ClaimExtractor`` の候補は和集合で上乗せ）
4. 結論（``VerificationVerdict``）:
   - Evidence の依頼が ``completed`` でない → ``insufficient``（評価器なし・枠切れの Evidence で
     合格させない）
   - 強すぎる表現・異説を事実として述べる・未登録の主張 → ``failed``
   - ``insufficient`` の claim に依拠する・評価していない claim に依拠する → ``insufficient``
   - それ以外 → ``passed``

照合は語の重なりによる**最低限の網**。過検出は追加調査・修正へ倒れる安全側、見逃しは Extractor で
減らす。比較は同じ言語の文面どうし（翻訳しない）。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from contracts.research import ResearchStatus
from contracts.research_evidence import (
    FAILING_CHECK_OUTCOMES,
    MAX_CLAIMS_PER_UNIT,
    CheckOutcome,
    ClaimAssessment,
    EvidenceArtifact,
    EvidenceClaim,
    ScriptVerificationRequest,
    VerificationVerdict,
    build_script_verification_artifact,
)
from domain.artifact.hashing import sha256_hex
from domain.research.evidence_ports import CandidateSentence
from domain.research.evidence_text import (
    extract_keywords,
    find_year_spans,
    has_dispute_terms,
    is_approximate,
    is_hedged,
    nfkc,
    numbers_in,
    quantifier_level,
    quoted_spans,
    squash,
    strength_terms,
)

__all__ = [
    "MIN_SHARED_FEATURES",
    "TRIGGER_EXTRACTOR",
    "detect_candidates",
    "split_sentences",
    "unit_text_sha256",
    "verify_script",
]

#: 文と claim が対応するために共有すべき特徴（語・数値）の最小数。
MIN_SHARED_FEATURES = 2
TRIGGER_EXTRACTOR = "extractor"
_REASON_MAX = 300
_SENTENCE_END = frozenset("。！？!?.")
_CLOSERS = frozenset("」』）)”\"’'］]")


def unit_text_sha256(text: str) -> str:
    """``VerificationUnit.text_sha256`` の唯一の計算（最終文面の UTF-8 の sha256）。"""
    return sha256_hex(text.encode("utf-8"))


def split_sentences(text: str) -> list[str]:
    """文に分ける（``。！？!?``・直後が空白か終端の ``.``・改行）。閉じ括弧は前の文に付ける。"""
    sentences: list[str] = []
    buffer: list[str] = []
    n = len(text)
    i = 0

    def flush() -> None:
        sentence = "".join(buffer).strip()
        if sentence:
            sentences.append(sentence)
        buffer.clear()

    while i < n:
        ch = text[i]
        if ch == "\n":
            flush()
            i += 1
            continue
        buffer.append(ch)
        if ch in _SENTENCE_END and (
            ch != "." or i + 1 >= n or text[i + 1].isspace() or text[i + 1] in _CLOSERS
        ):
            while i + 1 < n and (text[i + 1] in _SENTENCE_END or text[i + 1] in _CLOSERS):
                i += 1
                buffer.append(text[i])
            flush()
        i += 1
    flush()
    return sentences


def _key(sentence: str) -> str:
    return " ".join(sentence.split())


def _triggers(sentence: str) -> tuple[str, ...]:
    found: list[str] = []
    if find_year_spans(sentence):
        found.append("year")
    if numbers_in(sentence) and not find_year_spans(sentence):
        found.append("quantity")
    terms = strength_terms(sentence)
    found.extend(sorted(terms))
    if quoted_spans(sentence):
        found.append("quote")
    return tuple(dict.fromkeys(found))


def detect_candidates(units: Iterable[tuple[str, str]]) -> list[CandidateSentence]:
    """**決定的な最低限の網**。主張らしい徴候がある文を返す（``(unit_id, text)`` の列を受ける）。"""
    found: list[CandidateSentence] = []
    for unit_id, text in units:
        for sentence in split_sentences(text):
            triggers = _triggers(sentence)
            if triggers:
                found.append(CandidateSentence(unit_id, sentence, triggers))
    return found


def _features(text: str) -> frozenset[str]:
    return frozenset(k.casefold() for k in extract_keywords(text, limit=32)) | numbers_in(text)


@dataclass(frozen=True, slots=True)
class _Indexed:
    claim: EvidenceClaim
    features: frozenset[str]


def _matches(sentence: str, claims: Sequence[_Indexed]) -> list[_Indexed]:
    """文が依拠する claim。最も重なる claim を主にし、主が説明しない特徴を持つものだけ加える。"""
    features = _features(sentence)
    if not features:
        return []
    scored = sorted(
        (
            (len(features & c.features), c)
            for c in claims
            if len(features & c.features) >= MIN_SHARED_FEATURES
        ),
        key=lambda item: (-item[0], item[1].claim.claim_id),
    )
    found: list[_Indexed] = []
    remaining = set(features)
    for _, indexed in scored:
        if found and not (remaining & indexed.features):
            continue
        found.append(indexed)
        remaining -= indexed.features
    return found


def _check(sentence: str, claim: EvidenceClaim) -> tuple[CheckOutcome, list[str]]:
    if not claim.assessed or claim.assessment is ClaimAssessment.INSUFFICIENT:
        return CheckOutcome.UNSUPPORTED, ["the claim has no usable evidence; it cannot be stated"]
    usable = claim.usable_expression
    reasons: list[str] = []
    if claim.assessment is ClaimAssessment.DISPUTED and not (
        has_dispute_terms(sentence) or is_hedged(sentence)
    ):
        return CheckOutcome.DISPUTED_AS_FACT, ["a disputed claim must be presented as contested"]
    if quantifier_level(sentence) > max(quantifier_level(usable), 2):
        reasons.append("quantifier stronger than the evidence")
    extra_terms = strength_terms(sentence) - strength_terms(usable)
    if extra_terms:
        reasons.append(f"stronger than the evidence: {', '.join(sorted(extra_terms))}")
    extra_numbers = numbers_in(sentence) - numbers_in(usable)
    if extra_numbers:
        reasons.append(f"number(s) not in the evidence: {', '.join(sorted(extra_numbers))}")
    if (
        numbers_in(sentence) & numbers_in(usable)
        and is_approximate(usable)
        and not is_approximate(sentence)
    ):
        reasons.append("an approximate figure is stated as exact")
    if (
        claim.assessment is ClaimAssessment.QUALIFIED
        and is_hedged(usable)
        and not is_hedged(sentence)
    ):
        reasons.append("a qualified claim is stated with more certainty than the evidence")
    if reasons:
        return CheckOutcome.OVERSTATED, reasons
    return CheckOutcome.OK, []


def _quotes_backed(sentence: str, matches: Sequence[_Indexed]) -> bool:
    for quote in quoted_spans(sentence):
        needle = squash(quote)
        if not any(
            m.claim.assessment is ClaimAssessment.SUPPORTED
            and needle in squash(f"{m.claim.usable_expression} {m.claim.text}")
            for m in matches
        ):
            return False
    return True


def _cap(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= _REASON_MAX else text[: _REASON_MAX - 1] + "…"


def verify_script(
    request: ScriptVerificationRequest,
    evidence: EvidenceArtifact,
    *,
    evidence_status: ResearchStatus,
    extra_candidates: Sequence[CandidateSentence] = (),
) -> dict[str, Any]:
    """台本を照合し、``research_script_verification`` の payload（契約を通したもの）を返す。"""
    if evidence.request_id != request.evidence_request_id:
        raise ValueError("the evidence artifact belongs to a different research request")
    units = [(u.unit_id, u.text) for u in request.units]
    known = {u for u, _ in units}
    candidates: dict[tuple[str, str], CandidateSentence] = {}
    for cand in [*detect_candidates(units), *extra_candidates]:
        if cand.unit_id in known and cand.sentence.strip():
            candidates.setdefault((cand.unit_id, _key(cand.sentence)), cand)
    indexed = [_Indexed(c, _features(f"{c.text} {c.usable_expression}")) for c in evidence.claims]

    unit_rows: list[dict[str, Any]] = []
    unregistered: list[dict[str, str]] = []
    outcomes: list[CheckOutcome] = []
    for unit_id, text in units:
        sentences = {_key(s): s for s in split_sentences(text)}
        for (cand_unit, key), cand in candidates.items():
            if cand_unit == unit_id:
                sentences.setdefault(key, cand.sentence)
        per_claim: dict[str, tuple[CheckOutcome, list[str]]] = {}
        for key, sentence in sentences.items():
            matches = _matches(sentence, indexed)
            for m in matches:
                outcome, reasons = _check(sentence, m.claim)
                previous = per_claim.get(m.claim.claim_id)
                if previous is None or (
                    previous[0] is CheckOutcome.OK and outcome is not CheckOutcome.OK
                ):
                    per_claim[m.claim.claim_id] = (outcome, reasons)
            cand = candidates.get((unit_id, key))
            if cand is None:
                continue
            reason: str | None = None
            if not matches:
                reason = f"no evidence claim matches this sentence ({', '.join(cand.triggers)})"
            elif quoted_spans(sentence) and not _quotes_backed(sentence, matches):
                reason = "quoted speech is not backed by a supported claim"
            if reason is not None:
                unregistered.append(
                    {"unit_id": unit_id, "sentence": nfkc(sentence)[:500], "reason": _cap(reason)}
                )
        checks = [
            {
                "claim_id": cid,
                "outcome": per_claim[cid][0].value,
                "reason": _cap("; ".join(per_claim[cid][1])),
            }
            for cid in sorted(per_claim)[:MAX_CLAIMS_PER_UNIT]
        ]
        outcomes.extend(per_claim[cid][0] for cid in sorted(per_claim))
        unit_rows.append(
            {"unit_id": unit_id, "text_sha256": unit_text_sha256(text), "checks": checks}
        )

    reasons: list[str] = []
    if evidence_status is not ResearchStatus.COMPLETED:
        verdict = VerificationVerdict.INSUFFICIENT
        reasons.append(f"the evidence request is {evidence_status.value}, not completed")
    elif unregistered or any(o in FAILING_CHECK_OUTCOMES for o in outcomes):
        verdict = VerificationVerdict.FAILED
    elif any(o is CheckOutcome.UNSUPPORTED for o in outcomes):
        verdict = VerificationVerdict.INSUFFICIENT
    else:
        verdict = VerificationVerdict.PASSED
    if unregistered:
        reasons.append(f"{len(unregistered)} unregistered claim(s)")
    for outcome in (
        CheckOutcome.OVERSTATED,
        CheckOutcome.DISPUTED_AS_FACT,
        CheckOutcome.UNSUPPORTED,
    ):
        count = sum(1 for o in outcomes if o is outcome)
        if count:
            reasons.append(f"{count} check(s) {outcome.value}")

    return build_script_verification_artifact(
        request_id=request.evidence_request_id,
        source_evidence=request.evidence_artifact.model_dump(mode="json"),
        evidence_status=evidence_status.value,
        script_ref=request.script_ref.model_dump(mode="json") if request.script_ref else None,
        episode_id=request.episode_id,
        language=request.language,
        units=unit_rows,
        unregistered=_dedupe(unregistered)[:100],
        verdict=verdict.value,
        reasons=reasons[:20],
    )


def _dedupe(items: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[tuple[str, str]] = set()
    unique: list[dict[str, str]] = []
    for item in items:
        key = (item["unit_id"], _key(item["sentence"]))
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique
