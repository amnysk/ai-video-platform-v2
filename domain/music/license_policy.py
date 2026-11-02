"""自動承認の規則 ``decide_license``（ADR-0087 §2、INV-150 / INV-151）。純関数。

``AUTO_APPROVE`` になるのは次が**全部**成り立つときだけ:

- ソースが ``auto_acquire_allowed``（``contracts.music_sources.SOURCE_POLICIES``）
- ``LicenseShortName`` と ``License``（機械可読の識別子）が正規化後に**どちらも CC0 1.0**
  （片方だけ・食い違いは不可）
- ``LicenseUrl`` が CC0 1.0 の公開宣言、``AttributionRequired`` が明示的に false
- ``Credit`` が「Own work」（HTML 除去後に完全一致。余計な文字・危険なマークアップは不可）
- ``Restrictions`` が空、削除・係争の印が無い、作者名と原本 URL がある

曖昧なものは**倒す側**（REVIEW / REJECT）へ。理由コードは固定語彙（``LicenseReason``）。
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime

from contracts.music import BgmTrack, LicenseType, is_blank_text
from contracts.music_sources import (
    BGM_LICENSE_POLICY_VERSION,
    SOURCE_POLICIES,
    LicenseDecision,
    LicenseDecisionKind,
    LicenseFacts,
    LicenseReason,
)
from contracts.upload import YOUTUBE_FORBIDDEN_CHARS

_TOKEN = re.compile(r"[^a-z0-9.]+")
_PLAIN = re.compile(r"[a-z0-9][a-z0-9 .\-_]*")
_CC0_FORMS = frozenset({"cc0", "cc0 1.0"})
_NC_TOKENS = frozenset({"nc", "noncommercial"})
_ND_TOKENS = frozenset({"nd", "noderivs", "noderivatives"})

_K = LicenseDecisionKind
_R = LicenseReason


def _tokens(text: str) -> list[str]:
    folded = unicodedata.normalize("NFKC", text).casefold()
    return [t for t in _TOKEN.split(folded) if t]


def _family(text: str | None) -> str | None:
    """ライセンス表記の系統。``None`` = 空、``"unknown"`` = 読めない・曖昧。"""
    if text is None or is_blank_text(text):
        return None
    # 句読点・記号の混じる表記（"CC0?" "PD / CC0" 等）は曖昧として読まない
    if not _PLAIN.fullmatch(unicodedata.normalize("NFKC", text).strip().casefold()):
        return "unknown"
    tokens = _tokens(text)
    if not tokens:
        return "unknown"
    joined = " ".join(tokens)
    if any(t.startswith("cc0") or t == "zero" for t in tokens):
        return "cc0" if joined in _CC0_FORMS else "unknown"
    if tokens[0] == "cc" and "by" in tokens:
        return "by-sa" if "sa" in tokens else "by"
    if (tokens[0] == "pd" or joined.startswith("public domain")) and "by" not in tokens:
        return "pd" if "cc0" not in joined else "unknown"
    return "unknown"


def _has_marker(facts: LicenseFacts, markers: frozenset[str], phrases: tuple[str, ...]) -> bool:
    for text in (
        facts.license_short_name,
        facts.license_code,
        facts.license_url,
        facts.usage_terms,
    ):
        if not text:
            continue
        tokens = _tokens(text)
        if any(t in markers for t in tokens):
            return True
        folded = unicodedata.normalize("NFKC", text).casefold()
        if any(p in folded for p in phrases):
            return True
    return False


def is_own_work(credit: str | None) -> bool:
    if credit is None:
        return False
    text = unicodedata.normalize("NFKC", credit).strip().casefold().rstrip(".").strip()
    return text == "own work"


def _blank(value: str | None) -> bool:
    return value is None or is_blank_text(value)


def decide_license(facts: LicenseFacts) -> LicenseDecision:
    policy = SOURCE_POLICIES.get(facts.source)
    if policy is None or not policy.search_allowed:
        return LicenseDecision(_K.REJECT, (_R.SOURCE_UNKNOWN,))
    if facts.deletion_or_dispute:
        return LicenseDecision(_K.REJECT, (_R.DELETION_OR_DISPUTE,))
    reject: list[LicenseReason] = []
    if _has_marker(facts, _NC_TOKENS, ("non-commercial", "non commercial", "noncommercial")):
        reject.append(_R.NON_COMMERCIAL)
    if _has_marker(facts, _ND_TOKENS, ("no derivative", "noderiv")):
        reject.append(_R.NO_DERIVATIVES)
    if reject:
        return LicenseDecision(_K.REJECT, tuple(reject))

    short = _family(facts.license_short_name)
    code = _family(facts.license_code)
    if short == "unknown" or code == "unknown" or (short is None and code is None):
        return LicenseDecision(_K.REJECT, (_R.LICENSE_UNKNOWN,))
    if short is not None and code is not None and short != code:
        return LicenseDecision(_K.REJECT, (_R.METADATA_CONFLICT,))
    family = short or code
    if family == "cc0" and facts.attribution_required is True:
        return LicenseDecision(_K.REJECT, (_R.METADATA_CONFLICT,))
    url = (facts.license_url or "").casefold()
    if family == "cc0" and url and "publicdomain/zero/1.0" not in url:
        return LicenseDecision(_K.REJECT, (_R.METADATA_CONFLICT,))

    review: list[LicenseReason] = []
    if not policy.auto_acquire_allowed:
        review.append(_R.SOURCE_NOT_AUTO_ACQUIRABLE)
    if family == "by":
        review.append(_R.ATTRIBUTION_LICENSE_NEEDS_REVIEW)
    elif family == "by-sa":
        review.append(_R.SHARE_ALIKE_NEEDS_REVIEW)
    elif family == "pd":
        review.append(_R.PUBLIC_DOMAIN_NEEDS_REVIEW)
    else:  # cc0
        incomplete = (
            short is None
            or code is None
            or _blank(facts.license_url)
            or facts.attribution_required is None
            or _blank(facts.artist)
            or _blank(facts.original_file_url)
            or facts.restrictions is None
        )
        if incomplete or _blank(facts.credit):
            review.append(_R.METADATA_INCOMPLETE)
        elif not is_own_work(facts.credit):
            review.append(_R.NOT_OWN_WORK)
        if not _blank(facts.restrictions):
            review.append(_R.RESTRICTIONS_PRESENT)
    if review:
        return LicenseDecision(_K.REVIEW, tuple(review))
    return LicenseDecision(_K.AUTO_APPROVE, (_R.CC0_OWN_WORK,))


# ---------------------------------------------------------------- BgmTrack の組み立て


def track_id_for_sha256(file_sha256: str) -> str:
    return f"bgm-wmc-{file_sha256[:16]}"


def _license_type(facts: LicenseFacts) -> LicenseType:
    family = _family(facts.license_short_name) or _family(facts.license_code)
    return {"cc0": "cc0", "by": "cc_by"}.get(family or "", "custom")  # type: ignore[return-value]


def _clean(text: str) -> str:
    return "".join(ch for ch in text if ch not in YOUTUBE_FORBIDDEN_CHARS).strip()


def _attribution_text(facts: LicenseFacts, title: str, artist: str) -> str:
    parts = [f'"{title}" by {artist}']
    if facts.license_short_name:
        parts.append(f"({facts.license_short_name})")
    if facts.license_url:
        parts.append(facts.license_url)
    parts.append(facts.source_page_url)
    return _clean(" ".join(parts))


def build_quarantined_track(
    facts: LicenseFacts,
    *,
    file_sha256: str,
    object_key: str,
    duration_ms: int,
    loudness_lufs: float | None,
    bpm: int | None,
) -> BgmTrack:
    """登録用の曲。**常に** ``quarantined`` + ``unverified``（承認は ``approve_track`` だけ）。"""
    title = _clean(facts.title or "") or "untitled"
    artist = _clean(facts.artist or "") or "unknown"
    family = _family(facts.license_short_name) or _family(facts.license_code)
    attribution = bool(facts.attribution_required) or family in {"by", "by-sa"}
    return BgmTrack(
        track_id=track_id_for_sha256(file_sha256),
        file_sha256=file_sha256,
        object_key=object_key,
        title=title,
        artist=artist,
        source_url=facts.source_page_url,
        source_kind="licensed_external",
        license_type=_license_type(facts),
        license_status="unverified",
        allowed_platforms=("youtube",),
        allowed_formats=("shorts", "longform"),
        commercial_use_allowed=True,
        reuse_allowed=True,
        modification_allowed=True,
        attribution_required=attribution,
        exact_attribution_text=_attribution_text(facts, title, artist) if attribution else None,
        energy="medium",
        bpm=bpm,
        duration_ms=duration_ms,
        loudness_lufs=loudness_lufs,
        status="quarantined",
        content_id_risk="unknown",
    )


def approve_track(
    track: BgmTrack, facts: LicenseFacts, decision: LicenseDecision, *, checked_at: datetime
) -> BgmTrack:
    """``promote`` に渡す承認済みの版。規則を**もう一度**適用し、AUTO_APPROVE 以外は拒否する。"""
    again = decide_license(facts)
    if decision.kind is not _K.AUTO_APPROVE or again != decision:
        raise ValueError("approve_track needs an AUTO_APPROVE decision for these facts")
    if track.status != "quarantined" or track.license_status != "unverified":
        raise ValueError("approve_track needs a quarantined, unverified track")
    data = track.model_dump()
    data.update(
        license_type="cc0",
        license_status="approved",
        license_evidence_sha256=facts.evidence_sha256,
        license_checked_at=checked_at,
        attribution_required=False,
        exact_attribution_text=None,
        license_approver_kind="policy",
        license_policy_version=BGM_LICENSE_POLICY_VERSION,
        status="active",
    )
    return BgmTrack.model_validate(data)


__all__ = [
    "approve_track",
    "build_quarantined_track",
    "decide_license",
    "is_own_work",
    "track_id_for_sha256",
]
