"""Evidence Research と台本の照合の契約（ADR-0038）。**この 2 つの成果物の唯一の宣言元**。

research が所有する 2 つの成果物（``research_artifacts``。型は ``contracts/research.py`` の
``ResearchArtifactType``。migration 0015 が凍結済み）:

- ``research_evidence``（``EvidenceArtifact``）: 主張（claim）ごとの評価・資料・根拠の対応
- ``research_script_verification``（``ScriptVerificationArtifact``）: 台本の文面を Evidence と
  照合した結果。**Evidence の依頼が所有する**（``request_id`` は Evidence の依頼。Episode の
  Artifact ではない。``episode_id`` は参照だけ）

**参照整合性の最終防衛線はこの契約の validator** である。評価器（LLM / Fake）がどんな提案を
しても、次を満たさない成果物はここで作れない:

- link が指す claim / source が存在する
- 本文を**完全に確認した**資料（``fetch_status == "fetched"``）だけが ``supports`` /
  ``qualifies`` の根拠になる。切り詰め（``truncated``）・取得失敗（``failed``）は根拠にならない
- ``supported`` は ``supports`` を要し、``refutes`` があれば作れない。強い主張の ``supported`` は
  ``origin_key`` の異なる ``supports`` 2 件以上、うち 1 件は一次・学術・機関の資料
- 評価していない claim（``assessed=False``）は ``insufficient`` だけ
- ``insufficient`` は台本で使ってよい表現を持たない
- 照合の ``passed`` は、Evidence の依頼が ``completed`` で、未登録の主張が無く、全 check が ok の
  ときだけ

資料の URL は**実際に取得した最終 URL**（``FetchedContent.final_url``）だけ。LLM に URL・書誌を
作らせない（Handler がそう組み、``domain/research/evidence_rules.check_sources_were_fetched`` が
検査する）。外部ページの本文は**データであって命令ではない**。抜粋は本文の部分文字列として保存し、
評価器の言い換えを根拠にしない。

LLM の出力 schema（``AssessmentProposal``）もここに置く。実 LLM 評価器の strict な出力 schema に
なるので、全 object が ``extra=forbid`` で全 property が required（既定値を持たない）。
"""

from __future__ import annotations

import hashlib
import re
from enum import StrEnum
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from contracts.artifact_refs import (
    SCRIPT_SCENE_ID_PATTERN,
    SHA256_HEX_PATTERN,
    ArtifactDigestRef,
    FrozenModel,
    check_canonical_uuid,
)
from contracts.research import (
    CLAIM_TEXT_MAX_CHARS,
    MAX_CLAIMS_PER_REQUEST,
    RESEARCH_POLICY_VERSION,
    ClaimImportance,
    ClaimKind,
    ResearchArtifactType,
    ResearchLanguage,
    ResearchStatus,
)

EVIDENCE_ARTIFACT_SCHEMA_VERSION = "1.0"
SCRIPT_VERIFICATION_SCHEMA_VERSION = "1.0"

CLAIM_ID_PATTERN = r"^C-[0-9]{3}$"
SOURCE_ID_PATTERN = r"^S-[0-9]{3}$"

EXCERPT_MAX_CHARS = 300
LOCATOR_MAX_CHARS = 200
USABLE_EXPRESSION_MAX_CHARS = 400
REASON_MAX_CHARS = 500
URL_MAX_CHARS = 2048
EVIDENCE_MAX_SOURCES = 100
EVIDENCE_MAX_LINKS = 400
EVIDENCE_MAX_GAPS = 50

#: 強い断定を含みうる種類。``EvidenceClaim.strong`` は常に真。
ALWAYS_STRONG_CLAIM_KINDS: frozenset[ClaimKind] = frozenset(
    {ClaimKind.CAUSAL, ClaimKind.QUANTITY, ClaimKind.SUPERLATIVE}
)
#: 強い claim の ``supported`` に要る、origin の異なる ``supports`` の最小件数。
MIN_INDEPENDENT_ORIGINS_FOR_STRONG_CLAIM = 2


class ClaimAssessment(StrEnum):
    """claim の評価。**依頼の実行状態（``ResearchStatus``）とは別の軸**。

    強い順: supported > qualified > disputed > insufficient。
    """

    SUPPORTED = "supported"
    QUALIFIED = "qualified"
    DISPUTED = "disputed"
    INSUFFICIENT = "insufficient"


class SourceFetchStatus(StrEnum):
    """資料の取得の結果（``FetchedContent`` から決まる。snippet だけの資料は載せない）。"""

    #: 本文を完全に取得し、テキストとして読めた（``FetchedContent.body_confirmed``）
    FETCHED = "fetched"
    #: 取得したが本文を確認できない（切り詰め・テキスト化できない・空本文）
    TRUNCATED = "truncated"
    FAILED = "failed"


class Stance(StrEnum):
    SUPPORTS = "supports"
    QUALIFIES = "qualifies"
    REFUTES = "refutes"


class SourceKind(StrEnum):
    PRIMARY = "primary"
    SCHOLARLY = "scholarly"
    INSTITUTIONAL = "institutional"
    REFERENCE = "reference"
    SECONDARY = "secondary"


#: 強い claim の独立した裏付けのうち 1 件が属すべき資料の種類。
AUTHORITATIVE_SOURCE_KINDS: frozenset[SourceKind] = frozenset(
    {SourceKind.PRIMARY, SourceKind.SCHOLARLY, SourceKind.INSTITUTIONAL}
)


def excerpt_digest(excerpt: str) -> str:
    """``EvidenceLink.excerpt_sha256`` の唯一の計算（UTF-8 の sha256）。"""
    return hashlib.sha256(excerpt.encode("utf-8")).hexdigest()


def validate_http_url(value: str) -> str:
    """http(s) の絶対 URL（host あり・認証情報なし・長さの上限）。"""
    if len(value) > URL_MAX_CHARS or any(ch.isspace() for ch in value):
        raise ValueError("url must be at most 2048 characters without whitespace")
    try:
        parts = urlsplit(value)
        host = parts.hostname
    except ValueError as exc:
        raise ValueError(f"invalid url: {value[:80]!r}") from exc
    if parts.scheme not in ("http", "https") or not host:
        raise ValueError("url must be an absolute http(s) URL")
    if parts.username is not None or parts.password is not None:
        raise ValueError("url must not carry credentials")
    return value


def _non_blank_items(value: tuple[str, ...], limit: int, name: str) -> tuple[str, ...]:
    if any(not item.strip() or len(item) > limit for item in value):
        raise ValueError(f"{name} must be non-blank and at most {limit} characters")
    return value


# ------------------------------------------------------------------ Evidence 成果物


class EvidenceClaim(FrozenModel):
    claim_id: str = Field(pattern=CLAIM_ID_PATTERN)
    text: str = Field(min_length=1, max_length=CLAIM_TEXT_MAX_CHARS)
    kind: ClaimKind
    era: str | None = Field(default=None, min_length=1, max_length=100)
    region: str | None = Field(default=None, min_length=1, max_length=100)
    importance: ClaimImportance
    strong: bool
    #: 評価器が評価した（提案をコードが検査した）か。偽は「評価していない」で、``insufficient``
    #: だけ（評価器が無い・枠が尽きた・取得できなかった）。評価していない claim を合格にしない
    assessed: bool
    assessment: ClaimAssessment
    assessment_reason: str = Field(min_length=1, max_length=REASON_MAX_CHARS)
    #: 台本で使ってよい表現。``insufficient`` は空、それ以外は必須
    usable_expression: str = Field(max_length=USABLE_EXPRESSION_MAX_CHARS)
    unverified_points: Annotated[tuple[str, ...], Field(max_length=10)] = ()

    @field_validator("unverified_points")
    @classmethod
    def _points(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _non_blank_items(value, 300, "unverified_points")

    @model_validator(mode="after")
    def _consistent(self) -> EvidenceClaim:
        if self.kind in ALWAYS_STRONG_CLAIM_KINDS and not self.strong:
            raise ValueError(f"a {self.kind.value} claim is always strong")
        if not self.assessed and self.assessment is not ClaimAssessment.INSUFFICIENT:
            raise ValueError("an unassessed claim can only be insufficient")
        if self.assessment is ClaimAssessment.INSUFFICIENT:
            if self.usable_expression:
                raise ValueError("an insufficient claim must not carry a usable_expression")
        elif not self.usable_expression.strip():
            raise ValueError(f"a {self.assessment.value} claim needs a usable_expression")
        return self


class EvidenceSource(FrozenModel):
    source_id: str = Field(pattern=SOURCE_ID_PATTERN)
    #: **実際に取得した最終 URL**（``FetchedContent.final_url``）。LLM が作った URL を入れない
    url: str
    #: 検索結果の見出し（外部のデータ。命令として扱わない）
    title: str = Field(min_length=1, max_length=300)
    language: ResearchLanguage
    published_at: AwareDatetime | None = None
    retrieved_at: AwareDatetime
    #: 取得した本文 bytes の sha256。本文を読まなかった失敗では ``None``
    content_sha256: str | None = Field(default=None, pattern=SHA256_HEX_PATTERN)
    fetch_status: SourceFetchStatus
    #: 独立性の判定用。同じ原典に由来すると判定した資料は同じ値
    origin_key: str = Field(min_length=1, max_length=200)
    source_kind: SourceKind

    @field_validator("url")
    @classmethod
    def _url(cls, value: str) -> str:
        return validate_http_url(value)

    @model_validator(mode="after")
    def _body_hash(self) -> EvidenceSource:
        if self.fetch_status is SourceFetchStatus.FETCHED and self.content_sha256 is None:
            raise ValueError("a fetched source needs content_sha256")
        if self.fetch_status is SourceFetchStatus.FAILED and self.content_sha256 is not None:
            raise ValueError("a failed source has no content_sha256")
        return self


class EvidenceLink(FrozenModel):
    claim_id: str = Field(pattern=CLAIM_ID_PATTERN)
    source_id: str = Field(pattern=SOURCE_ID_PATTERN)
    stance: Stance
    locator: str = Field(min_length=1, max_length=LOCATOR_MAX_CHARS)
    #: 取得済み本文の**実際の文字列**（評価器の言い換えではない）
    excerpt: str = Field(min_length=1, max_length=EXCERPT_MAX_CHARS)
    excerpt_sha256: str = Field(pattern=SHA256_HEX_PATTERN)

    @model_validator(mode="after")
    def _excerpt(self) -> EvidenceLink:
        if not self.excerpt.strip():
            raise ValueError("excerpt must not be blank")
        if excerpt_digest(self.excerpt) != self.excerpt_sha256:
            raise ValueError("excerpt_sha256 does not match the excerpt")
        return self


class EvidenceGap(FrozenModel):
    """未確認の論点・追加調査の候補。"""

    claim_id: str | None = Field(default=None, pattern=CLAIM_ID_PATTERN)
    description: str = Field(min_length=1, max_length=300)
    suggested_query: str | None = Field(default=None, min_length=1, max_length=200)


class EvidenceArtifact(FrozenModel):
    """根拠調査の結果（research 所有。``episode_id`` を持たない）。"""

    request_id: str
    type: Literal[ResearchArtifactType.RESEARCH_EVIDENCE]
    schema_version: Literal["1.0"]
    #: 評価規則の版。同じ入力でも版が違えば評価が変わりうる
    policy_version: str = Field(min_length=1, max_length=64)
    as_of: AwareDatetime
    claims: Annotated[
        tuple[EvidenceClaim, ...], Field(min_length=1, max_length=MAX_CLAIMS_PER_REQUEST)
    ]
    sources: Annotated[tuple[EvidenceSource, ...], Field(max_length=EVIDENCE_MAX_SOURCES)] = ()
    links: Annotated[tuple[EvidenceLink, ...], Field(max_length=EVIDENCE_MAX_LINKS)] = ()
    gaps: Annotated[tuple[EvidenceGap, ...], Field(max_length=EVIDENCE_MAX_GAPS)] = ()

    @field_validator("request_id")
    @classmethod
    def _request_id(cls, value: str) -> str:
        return check_canonical_uuid(value)

    @model_validator(mode="after")
    def _referential_integrity(self) -> EvidenceArtifact:
        claims = {c.claim_id: c for c in self.claims}
        if len(claims) != len(self.claims):
            raise ValueError("duplicate claim_id")
        sources = {s.source_id: s for s in self.sources}
        if len(sources) != len(self.sources):
            raise ValueError("duplicate source_id")

        links_by_claim: dict[str, list[EvidenceLink]] = {cid: [] for cid in claims}
        for link in self.links:
            if link.claim_id not in claims:
                raise ValueError(f"link references unknown claim_id {link.claim_id}")
            source = sources.get(link.source_id)
            if source is None:
                raise ValueError(f"link references unknown source_id {link.source_id}")
            if (
                link.stance in (Stance.SUPPORTS, Stance.QUALIFIES)
                and source.fetch_status is not SourceFetchStatus.FETCHED
            ):
                raise ValueError(
                    f"source {source.source_id} ({source.fetch_status.value}) cannot be the "
                    f"basis of a {link.stance.value} link"
                )
            links_by_claim[link.claim_id].append(link)

        for gap in self.gaps:
            if gap.claim_id is not None and gap.claim_id not in claims:
                raise ValueError(f"gap references unknown claim_id {gap.claim_id}")
        for claim in self.claims:
            if not claim.assessed and links_by_claim[claim.claim_id]:
                raise ValueError(f"{claim.claim_id}: an unassessed claim has no links")
            _check_assessment(claim, links_by_claim[claim.claim_id], sources)
        return self


def _check_assessment(
    claim: EvidenceClaim, links: list[EvidenceLink], sources: dict[str, EvidenceSource]
) -> None:
    supporting = [link for link in links if link.stance is Stance.SUPPORTS]
    backing = [link for link in links if link.stance in (Stance.SUPPORTS, Stance.QUALIFIES)]
    refuting = [link for link in links if link.stance is Stance.REFUTES]

    if claim.assessment is ClaimAssessment.SUPPORTED:
        if not supporting:
            raise ValueError(f"{claim.claim_id}: supported needs at least one supports link")
        if refuting:
            raise ValueError(f"{claim.claim_id}: a claim with a refutes link cannot be supported")
        if claim.strong:
            origins = {sources[link.source_id].origin_key for link in supporting}
            if len(origins) < MIN_INDEPENDENT_ORIGINS_FOR_STRONG_CLAIM:
                raise ValueError(
                    f"{claim.claim_id}: a strong claim needs supports links from at least "
                    f"{MIN_INDEPENDENT_ORIGINS_FOR_STRONG_CLAIM} independent origins"
                )
            if not any(
                sources[link.source_id].source_kind in AUTHORITATIVE_SOURCE_KINDS
                for link in supporting
            ):
                raise ValueError(
                    f"{claim.claim_id}: a strong claim needs a primary, scholarly or "
                    "institutional source among its supports links"
                )
    elif claim.assessment in (ClaimAssessment.QUALIFIED, ClaimAssessment.DISPUTED) and not backing:
        raise ValueError(
            f"{claim.claim_id}: {claim.assessment.value} needs a supports or qualifies link"
        )


def build_evidence_artifact(
    *,
    request_id: str,
    as_of: str,
    claims: Any,
    sources: Any,
    links: Any,
    gaps: Any = (),
    policy_version: str = RESEARCH_POLICY_VERSION,
) -> dict[str, Any]:
    """生成側。**検証を通してから** JSON の dict を返す（不正な成果物を作れる唯一の入口を塞ぐ）。"""
    artifact = EvidenceArtifact.model_validate(
        {
            "request_id": request_id,
            "type": ResearchArtifactType.RESEARCH_EVIDENCE.value,
            "schema_version": EVIDENCE_ARTIFACT_SCHEMA_VERSION,
            "policy_version": policy_version,
            "as_of": as_of,
            "claims": claims,
            "sources": sources,
            "links": links,
            "gaps": gaps,
        }
    )
    return artifact.model_dump(mode="json")


def parse_evidence_artifact(payload: dict[str, Any]) -> EvidenceArtifact:
    """取り込み側。想定外の schema_version・余計なキーは推測せず ``ValidationError``。"""
    return EvidenceArtifact.model_validate(payload)


# ------------------------------------------------------------------ 評価器の出力（LLM の schema）


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProposedLink(_StrictModel):
    """評価器が提案する claim と資料の対応。``excerpt`` は passage の文を写す。"""

    source_id: str = Field(pattern=SOURCE_ID_PATTERN)
    #: 評価器が「この資料」と考える URL。取得した最終 URL と違えば提案ごと採用しない
    source_url: str = Field(min_length=1, max_length=URL_MAX_CHARS)
    stance: Stance
    locator: str = Field(min_length=1, max_length=LOCATOR_MAX_CHARS)
    excerpt: str = Field(min_length=1, max_length=EXCERPT_MAX_CHARS)


class AssessmentProposal(_StrictModel):
    """評価器の**提案**。確定ではない（``evidence_rules.py`` が検査して確定する）。"""

    claim_id: str = Field(pattern=CLAIM_ID_PATTERN)
    assessment: ClaimAssessment
    links: Annotated[tuple[ProposedLink, ...], Field(max_length=8)]
    #: 台本で使ってよい表現の提案。``insufficient`` は空文字列
    usable_expression: str = Field(max_length=USABLE_EXPRESSION_MAX_CHARS)
    reason: str = Field(min_length=1, max_length=REASON_MAX_CHARS)
    unverified_points: Annotated[
        tuple[Annotated[str, Field(min_length=1, max_length=300)], ...], Field(max_length=10)
    ]


# ------------------------------------------------------------------ 台本の照合

_SCENE_ID_BODY = SCRIPT_SCENE_ID_PATTERN.removeprefix("^").removesuffix("$")
#: 照合の単位: ``title`` / ``hook`` / ``scene:s1:narration`` / ``scene:s1:visual``
UNIT_ID_PATTERN = rf"^(title|hook|scene:{_SCENE_ID_BODY}:(narration|visual))$"
UNIT_TEXT_MAX_CHARS = 4000
VERIFICATION_MAX_UNITS = 40
VERIFICATION_MAX_UNREGISTERED = 100
MAX_CLAIMS_PER_UNIT = MAX_CLAIMS_PER_REQUEST


class VerificationVerdict(StrEnum):
    """照合の結論。``passed`` 以外は「合格ではない」。

    - ``passed``: Evidence が ``completed`` で、未登録の主張が無く、全 check が ok
    - ``failed``: 台本が根拠を越える（強すぎる表現・未登録の主張・異説を事実として述べる）
    - ``insufficient``: 根拠が足りず判断できない（Evidence が ``completed`` でない・評価していない
      claim がある・``insufficient`` の claim に依拠している）
    """

    PASSED = "passed"
    FAILED = "failed"
    INSUFFICIENT = "insufficient"


class CheckOutcome(StrEnum):
    """文面 1 単位と claim 1 件の照合の結果。"""

    OK = "ok"
    #: 表現が Evidence の ``usable_expression`` より強い（量化子・因果・最上級・数値）
    OVERSTATED = "overstated"
    #: 異説（``disputed``）を異説として示さずに述べた
    DISPUTED_AS_FACT = "disputed_as_fact"
    #: ``insufficient`` の claim に依拠している（事実として述べられない）
    UNSUPPORTED = "unsupported"


#: 台本の問題（``failed`` にする）。``UNSUPPORTED`` は根拠の不足（``insufficient``）。
FAILING_CHECK_OUTCOMES: frozenset[CheckOutcome] = frozenset(
    {CheckOutcome.OVERSTATED, CheckOutcome.DISPUTED_AS_FACT}
)


class ScriptUnitInput(FrozenModel):
    """照合する台本の文面 1 単位（最終文面そのまま）。"""

    unit_id: str = Field(pattern=UNIT_ID_PATTERN)
    text: str = Field(max_length=UNIT_TEXT_MAX_CHARS)


class ScriptVerificationRequest(FrozenModel):
    """台本の照合の依頼。照合するのは台本の**文面**で、書き手が付けた claim id は受け取らない。"""

    #: Evidence の依頼（照合結果の所有者）
    evidence_request_id: str
    #: 照合に使う Evidence の成果物（その依頼の現行の ``research_evidence``）
    evidence_artifact: ArtifactDigestRef
    language: ResearchLanguage
    units: Annotated[
        tuple[ScriptUnitInput, ...], Field(min_length=1, max_length=VERIFICATION_MAX_UNITS)
    ]
    #: 照合した台本の参照（本番の Artifact。参照だけで、research はそれを読まない）
    script_ref: ArtifactDigestRef | None = None
    #: 参照だけ（所有ではない。FK も張らない）
    episode_id: str | None = None

    @field_validator("evidence_request_id")
    @classmethod
    def _request_id(cls, value: str) -> str:
        return check_canonical_uuid(value)

    @field_validator("episode_id")
    @classmethod
    def _episode_id(cls, value: str | None) -> str | None:
        return None if value is None else check_canonical_uuid(value)

    @model_validator(mode="after")
    def _unique_units(self) -> ScriptVerificationRequest:
        ids = [u.unit_id for u in self.units]
        if len(set(ids)) != len(ids):
            raise ValueError("unit_id must be unique")
        return self


class ClaimCheck(FrozenModel):
    claim_id: str = Field(pattern=CLAIM_ID_PATTERN)
    outcome: CheckOutcome
    reason: str = Field(max_length=300)

    @model_validator(mode="after")
    def _reason(self) -> ClaimCheck:
        if self.outcome is not CheckOutcome.OK and not self.reason.strip():
            raise ValueError("a failed check needs a reason")
        return self


class VerificationUnit(FrozenModel):
    unit_id: str = Field(pattern=UNIT_ID_PATTERN)
    #: 照合した**最終文面**の sha256（UTF-8）
    text_sha256: str = Field(pattern=SHA256_HEX_PATTERN)
    checks: Annotated[tuple[ClaimCheck, ...], Field(max_length=MAX_CLAIMS_PER_UNIT)] = ()

    @model_validator(mode="after")
    def _unique_claims(self) -> VerificationUnit:
        ids = [c.claim_id for c in self.checks]
        if len(set(ids)) != len(ids):
            raise ValueError("a unit checks each claim once")
        return self


class UnregisteredClaim(FrozenModel):
    """どの claim にも対応しない、検証可能な主張の候補文。"""

    unit_id: str = Field(pattern=UNIT_ID_PATTERN)
    sentence: str = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=1, max_length=300)


class ScriptVerificationArtifact(FrozenModel):
    """台本の照合結果（Evidence の依頼が所有する research の成果物）。"""

    #: Evidence の依頼（所有者）
    request_id: str
    type: Literal[ResearchArtifactType.RESEARCH_SCRIPT_VERIFICATION]
    schema_version: Literal["1.0"]
    policy_version: str = Field(min_length=1, max_length=64)
    source_evidence: ArtifactDigestRef
    #: 照合した時点の Evidence の依頼の実行状態（``completed`` 以外は合格にしない）
    evidence_status: ResearchStatus
    script_ref: ArtifactDigestRef | None = None
    episode_id: str | None = None
    language: ResearchLanguage
    units: Annotated[
        tuple[VerificationUnit, ...], Field(min_length=1, max_length=VERIFICATION_MAX_UNITS)
    ]
    unregistered: Annotated[
        tuple[UnregisteredClaim, ...], Field(max_length=VERIFICATION_MAX_UNREGISTERED)
    ] = ()
    verdict: VerificationVerdict
    reasons: Annotated[tuple[str, ...], Field(max_length=20)] = ()

    @field_validator("request_id")
    @classmethod
    def _request_id(cls, value: str) -> str:
        return check_canonical_uuid(value)

    @field_validator("episode_id")
    @classmethod
    def _episode_id(cls, value: str | None) -> str | None:
        return None if value is None else check_canonical_uuid(value)

    @field_validator("reasons")
    @classmethod
    def _reasons(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _non_blank_items(value, 300, "reasons")

    @model_validator(mode="after")
    def _verdict_is_earned(self) -> ScriptVerificationArtifact:
        unit_ids = [u.unit_id for u in self.units]
        if len(set(unit_ids)) != len(unit_ids):
            raise ValueError("duplicate unit_id")
        known = set(unit_ids)
        for item in self.unregistered:
            if item.unit_id not in known:
                raise ValueError(f"unregistered claim references unknown unit {item.unit_id}")
        if self.verdict is VerificationVerdict.PASSED:
            if self.evidence_status is not ResearchStatus.COMPLETED:
                raise ValueError("only evidence from a completed request can pass a script")
            if self.unregistered:
                raise ValueError("a passed verification has no unregistered claims")
            if any(c.outcome is not CheckOutcome.OK for u in self.units for c in u.checks):
                raise ValueError("a passed verification needs every check ok")
        elif not self.reasons:
            raise ValueError(f"a {self.verdict.value} verification needs reasons")
        return self


def build_script_verification_artifact(**fields: Any) -> dict[str, Any]:
    """生成側。検証を通してから JSON の dict を返す。"""
    artifact = ScriptVerificationArtifact.model_validate(
        {
            "type": ResearchArtifactType.RESEARCH_SCRIPT_VERIFICATION.value,
            "schema_version": SCRIPT_VERIFICATION_SCHEMA_VERSION,
            "policy_version": RESEARCH_POLICY_VERSION,
            **fields,
        }
    )
    return artifact.model_dump(mode="json")


def parse_script_verification_artifact(payload: dict[str, Any]) -> ScriptVerificationArtifact:
    return ScriptVerificationArtifact.model_validate(payload)


UNIT_ID_RE = re.compile(UNIT_ID_PATTERN)

__all__ = [
    "ALWAYS_STRONG_CLAIM_KINDS",
    "AUTHORITATIVE_SOURCE_KINDS",
    "CLAIM_ID_PATTERN",
    "EVIDENCE_ARTIFACT_SCHEMA_VERSION",
    "EXCERPT_MAX_CHARS",
    "FAILING_CHECK_OUTCOMES",
    "MIN_INDEPENDENT_ORIGINS_FOR_STRONG_CLAIM",
    "SCRIPT_VERIFICATION_SCHEMA_VERSION",
    "SOURCE_ID_PATTERN",
    "UNIT_ID_PATTERN",
    "UNIT_ID_RE",
    "AssessmentProposal",
    "CheckOutcome",
    "ClaimAssessment",
    "ClaimCheck",
    "EvidenceArtifact",
    "EvidenceClaim",
    "EvidenceGap",
    "EvidenceLink",
    "EvidenceSource",
    "ProposedLink",
    "ScriptUnitInput",
    "ScriptVerificationArtifact",
    "ScriptVerificationRequest",
    "SourceFetchStatus",
    "SourceKind",
    "Stance",
    "UnregisteredClaim",
    "VerificationUnit",
    "VerificationVerdict",
    "build_evidence_artifact",
    "build_script_verification_artifact",
    "excerpt_digest",
    "parse_evidence_artifact",
    "parse_script_verification_artifact",
    "validate_http_url",
]
