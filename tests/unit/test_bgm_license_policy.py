"""``decide_license``（INV-150）。敵対的な表で「曖昧なものは自動承認しない」を固定する。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from contracts.music import BgmTrack
from contracts.music_sources import (
    BGM_LICENSE_POLICY_VERSION,
    LicenseDecisionKind,
    LicenseFacts,
    LicenseReason,
    MusicSourceId,
)
from domain.music.commons_facts import (
    build_evidence_payload,
    pages_from_response,
)
from domain.music.license_policy import (
    approve_track,
    build_quarantined_track,
    decide_license,
    track_id_for_sha256,
)
from infrastructure.music.commons_evidence import parse_evidence

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "bgm_commons"
A, R, X = LicenseDecisionKind.AUTO_APPROVE, LicenseDecisionKind.REVIEW, LicenseDecisionKind.REJECT
NOW = datetime(2026, 11, 2, tzinfo=UTC)


def _facts(**over: Any) -> LicenseFacts:
    base: dict[str, Any] = dict(
        source=MusicSourceId.WIKIMEDIA_COMMONS,
        source_page_url="https://commons.wikimedia.org/wiki/File:x.flac",
        original_file_url="https://upload.wikimedia.org/wikipedia/commons/x.flac",
        license_short_name="CC0",
        license_url="http://creativecommons.org/publicdomain/zero/1.0/deed.en",
        license_code="cc0",
        usage_terms="Creative Commons Zero, Public Domain Dedication",
        artist="Someone",
        credit="Own work",
        attribution_required=False,
        copyrighted=True,
        restrictions="",
        deletion_or_dispute=False,
        title="x",
        evidence_sha256="e" * 64,
    )
    base.update(over)
    return LicenseFacts(**base)


def _real(name: str) -> LicenseFacts:
    page = pages_from_response(json.loads((FIX / f"{name}.json").read_text("utf-8")))[0]
    return parse_evidence(build_evidence_payload(page, fetched_at=NOW.isoformat())).facts


def test_baseline_is_auto_approved() -> None:
    d = decide_license(_facts())
    assert d.kind is A
    assert d.reasons == (LicenseReason.CC0_OWN_WORK,)
    assert d.policy_version == BGM_LICENSE_POLICY_VERSION


@pytest.mark.parametrize(
    ("name", "kind"),
    [("ck61", A), ("kuniholm", A), ("piano051", A), ("eastern", R), ("shika", R)],
)
def test_real_commons_responses(name: str, kind: LicenseDecisionKind) -> None:
    assert decide_license(_real(name)).kind is kind


def test_real_review_reasons() -> None:
    assert (
        LicenseReason.ATTRIBUTION_LICENSE_NEEDS_REVIEW in decide_license(_real("eastern")).reasons
    )
    assert LicenseReason.PUBLIC_DOMAIN_NEEDS_REVIEW in decide_license(_real("shika")).reasons


# (説明, 上書き, 期待の kind, 期待の理由)
ADVERSARIAL = [
    ("CC0 with question mark", dict(license_short_name="CC0?"), X, LicenseReason.LICENSE_UNKNOWN),
    (
        "pd slash cc0",
        dict(license_short_name="Public domain / CC0"),
        X,
        LicenseReason.LICENSE_UNKNOWN,
    ),
    (
        "cc-zero short name not recognised",
        dict(license_short_name="cc-zero"),
        X,
        LicenseReason.LICENSE_UNKNOWN,
    ),
    (
        "cc by nc short",
        dict(license_short_name="CC BY-NC 4.0", license_code="cc-by-nc-4.0"),
        X,
        LicenseReason.NON_COMMERCIAL,
    ),
    (
        "nc only in url",
        dict(license_url="https://creativecommons.org/licenses/by-nc/4.0/"),
        X,
        LicenseReason.NON_COMMERCIAL,
    ),
    ("nd only in code", dict(license_code="cc-by-nd-4.0"), X, LicenseReason.NO_DERIVATIVES),
    (
        "noncommercial in usage terms",
        dict(usage_terms="Creative Commons Attribution-NonCommercial"),
        X,
        LicenseReason.NON_COMMERCIAL,
    ),
    ("short cc0 code pd", dict(license_code="pd"), X, LicenseReason.METADATA_CONFLICT),
    ("short cc0 code by", dict(license_code="cc-by-4.0"), X, LicenseReason.METADATA_CONFLICT),
    (
        "short by code cc0",
        dict(license_short_name="CC BY 4.0", license_code="cc0"),
        X,
        LicenseReason.METADATA_CONFLICT,
    ),
    (
        "cc0 attribution required",
        dict(attribution_required=True),
        X,
        LicenseReason.METADATA_CONFLICT,
    ),
    (
        "cc0 url is by",
        dict(license_url="https://creativecommons.org/licenses/by/4.0/"),
        X,
        LicenseReason.METADATA_CONFLICT,
    ),
    (
        "cc0 url is cc0 1.0 variant",
        dict(license_url="https://creativecommons.org/publicdomain/zero/1.0/"),
        A,
        LicenseReason.CC0_OWN_WORK,
    ),
    (
        "no license at all",
        dict(license_short_name=None, license_code=None, license_url=None, usage_terms=None),
        X,
        LicenseReason.LICENSE_UNKNOWN,
    ),
    (
        "empty license",
        dict(license_short_name="", license_code=""),
        X,
        LicenseReason.LICENSE_UNKNOWN,
    ),
    ("no code", dict(license_code=None), R, LicenseReason.METADATA_INCOMPLETE),
    ("no url", dict(license_url=None), R, LicenseReason.METADATA_INCOMPLETE),
    ("attribution unknown", dict(attribution_required=None), R, LicenseReason.METADATA_INCOMPLETE),
    ("no artist", dict(artist=None), R, LicenseReason.METADATA_INCOMPLETE),
    ("blank artist", dict(artist="  "), R, LicenseReason.METADATA_INCOMPLETE),
    ("no original url", dict(original_file_url=None), R, LicenseReason.METADATA_INCOMPLETE),
    ("empty credit", dict(credit=""), R, LicenseReason.METADATA_INCOMPLETE),
    ("no credit", dict(credit=None), R, LicenseReason.METADATA_INCOMPLETE),
    (
        "credit other source",
        dict(credit="https://example.com/track.mp3"),
        R,
        LicenseReason.NOT_OWN_WORK,
    ),
    (
        "own work plus extra",
        dict(credit="Own work from https://evil.example"),
        R,
        LicenseReason.NOT_OWN_WORK,
    ),
    ("not own work", dict(credit="Kevin MacLeod"), R, LicenseReason.NOT_OWN_WORK),
    (
        "unsafe markup marker",
        dict(credit="Own work [unsafe-markup]"),
        R,
        LicenseReason.NOT_OWN_WORK,
    ),
    ("restrictions", dict(restrictions="trademarked"), R, LicenseReason.RESTRICTIONS_PRESENT),
    ("deletion", dict(deletion_or_dispute=True), X, LicenseReason.DELETION_OR_DISPUTE),
    (
        "cc by",
        dict(
            license_short_name="CC BY 4.0",
            license_code="cc-by-4.0",
            license_url="https://creativecommons.org/licenses/by/4.0/",
            attribution_required=True,
        ),
        R,
        LicenseReason.ATTRIBUTION_LICENSE_NEEDS_REVIEW,
    ),
    (
        "cc by sa",
        dict(
            license_short_name="CC BY-SA 3.0",
            license_code="cc-by-sa-3.0",
            license_url="https://creativecommons.org/licenses/by-sa/3.0/",
            attribution_required=True,
        ),
        R,
        LicenseReason.SHARE_ALIKE_NEEDS_REVIEW,
    ),
    (
        "pd",
        dict(
            license_short_name="Public domain",
            license_code="pd",
            license_url=None,
            attribution_required=False,
        ),
        R,
        LicenseReason.PUBLIC_DOMAIN_NEEDS_REVIEW,
    ),
    ("lowercase cc0 short ok", dict(license_short_name="cc0"), A, LicenseReason.CC0_OWN_WORK),
    (
        "CC0 1.0 short ok",
        dict(license_short_name="CC0 1.0", license_code="cc0-1.0"),
        A,
        LicenseReason.CC0_OWN_WORK,
    ),
    ("own work lowercase ok", dict(credit="own work"), A, LicenseReason.CC0_OWN_WORK),
]


@pytest.mark.parametrize(
    ("label", "over", "kind", "reason"), ADVERSARIAL, ids=[a[0] for a in ADVERSARIAL]
)
def test_adversarial_table(
    label: str, over: dict, kind: LicenseDecisionKind, reason: LicenseReason
) -> None:
    d = decide_license(_facts(**over))
    assert d.kind is kind, (label, d)
    assert reason in d.reasons, (label, d)
    if kind is not A:
        assert LicenseReason.CC0_OWN_WORK not in d.reasons


@pytest.mark.parametrize(
    "source",
    [
        MusicSourceId.INCOMPETECH,
        MusicSourceId.PIXABAY_MUSIC,
        MusicSourceId.CCMIXTER,
        MusicSourceId.FREE_MUSIC_ARCHIVE,
    ],
)
def test_non_auto_acquirable_source_never_auto_approves(source: MusicSourceId) -> None:
    d = decide_license(_facts(source=source))
    assert d.kind is R and LicenseReason.SOURCE_NOT_AUTO_ACQUIRABLE in d.reasons


def test_unsearchable_source_is_rejected() -> None:
    d = decide_license(_facts(source=MusicSourceId.OWNER_SUPPLIED))
    assert d.kind is X and LicenseReason.SOURCE_UNKNOWN in d.reasons


def test_decision_is_deterministic_and_reasons_ordered() -> None:
    f = _facts(restrictions="x", credit="y")
    assert decide_license(f) == decide_license(f)


def test_only_one_input_combination_class_approves() -> None:
    """個々の事実を 1 つずつ壊すと、必ず AUTO_APPROVE から落ちる（全項目が判定に効いている）。"""
    broken = [
        dict(license_short_name="CC BY 4.0"), dict(license_code="pd"), dict(license_url=None),
        dict(credit="x"), dict(restrictions="x"), dict(deletion_or_dispute=True),
        dict(attribution_required=True), dict(attribution_required=None), dict(artist=None),
        dict(original_file_url=None), dict(source=MusicSourceId.INCOMPETECH),
    ]  # fmt: skip
    for over in broken:
        assert decide_license(_facts(**over)).kind is not A, over


# ---------------------------------------------------------------- BgmTrack の組み立て


def _track(facts: LicenseFacts, sha: str = "ab" * 32) -> BgmTrack:
    return build_quarantined_track(
        facts,
        file_sha256=sha,
        object_key=f"music/originals/{sha}.flac",
        duration_ms=120_000,
        loudness_lufs=-20.0,
        bpm=None,
    )


def test_track_id_is_deterministic_from_sha256() -> None:
    assert track_id_for_sha256("ab" * 32) == "bgm-wmc-" + "ab" * 8


def test_registered_track_is_always_quarantined_and_unverified() -> None:
    t = _track(_facts())
    assert t.status == "quarantined" and t.license_status == "unverified"
    assert t.license_approver_kind is None and t.license_policy_version is None
    assert t.track_id == "bgm-wmc-" + "ab" * 8
    assert t.object_key == "music/originals/" + "ab" * 32 + ".flac"
    assert t.content_id_risk == "unknown"


def test_attribution_licenses_register_with_exact_attribution_text() -> None:
    f = _facts(
        license_short_name="CC BY 3.0", license_code="cc-by-3.0", attribution_required=True,
        artist="Kevin <MacLeod>", title="Eastern Thought",
        license_url="https://creativecommons.org/licenses/by/3.0/",
    )  # fmt: skip
    t = _track(f)
    assert t.license_type == "cc_by" and t.attribution_required
    assert t.exact_attribution_text and "<" not in t.exact_attribution_text
    assert "Kevin" in t.exact_attribution_text and "CC BY 3.0" in t.exact_attribution_text
    assert t.status == "quarantined"


def test_approve_track_requires_an_auto_approve_decision() -> None:
    f = _facts()
    base = _track(f)
    ok = approve_track(base, f, decide_license(f), checked_at=NOW)
    assert ok.status == "active" and ok.license_status == "approved"
    assert ok.license_type == "cc0" and ok.license_approver_kind == "policy"
    assert ok.license_policy_version == BGM_LICENSE_POLICY_VERSION
    assert ok.license_evidence_sha256 == f.evidence_sha256 and ok.license_checked_at == NOW
    assert ok.attribution_required is False
    review = _facts(restrictions="x")
    with pytest.raises(ValueError):
        approve_track(_track(review), review, decide_license(review), checked_at=NOW)
    with pytest.raises(ValueError):  # 判定が別の事実のもの
        approve_track(base, f, decide_license(review), checked_at=NOW)
