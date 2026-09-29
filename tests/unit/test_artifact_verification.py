"""domain/artifact/verification.py の純粋関数（ADR-0033）。I/O無し、全 verdict の分岐。"""

from __future__ import annotations

from domain.artifact.verification import ArtifactVerdict, ArtifactVerificationFacts, decide_verdict


def _facts(**overrides: bool) -> ArtifactVerificationFacts:
    base: dict[str, bool] = {
        "descriptor_exists": True,
        "schema_valid": True,
        "media_required": False,
        "media_exists": True,
        "descriptor_content_verified": True,
        "media_content_verified": True,
        "profile_check_applicable": False,
        "profile_id_valid": True,
    }
    base.update(overrides)
    return ArtifactVerificationFacts(**base)


def test_all_checks_pass_is_reusable() -> None:
    assert decide_verdict(_facts()) is ArtifactVerdict.REUSABLE


def test_missing_descriptor_is_missing() -> None:
    assert decide_verdict(_facts(descriptor_exists=False)) is ArtifactVerdict.MISSING


def test_missing_descriptor_wins_over_everything_else() -> None:
    # 推測しない: descriptor が無ければ他の事実がどうであれ MISSING（安全側）
    facts = _facts(descriptor_exists=False, schema_valid=False, descriptor_content_verified=False)
    assert decide_verdict(facts) is ArtifactVerdict.MISSING


def test_invalid_schema_is_corrupt_schema() -> None:
    assert decide_verdict(_facts(schema_valid=False)) is ArtifactVerdict.CORRUPT_SCHEMA


def test_schema_failure_wins_over_content_checks() -> None:
    facts = _facts(schema_valid=False, descriptor_content_verified=False)
    assert decide_verdict(facts) is ArtifactVerdict.CORRUPT_SCHEMA


def test_missing_media_when_required_is_missing() -> None:
    facts = _facts(media_required=True, media_exists=False)
    assert decide_verdict(facts) is ArtifactVerdict.MISSING


def test_media_not_required_ignores_media_exists_flag() -> None:
    # media_required=False なら media_exists の値は無視される
    facts = _facts(media_required=False, media_exists=False)
    assert decide_verdict(facts) is ArtifactVerdict.REUSABLE


def test_descriptor_content_mismatch_is_corrupt_hash() -> None:
    assert decide_verdict(_facts(descriptor_content_verified=False)) is ArtifactVerdict.CORRUPT_HASH


def test_media_content_mismatch_when_required_is_corrupt_hash() -> None:
    facts = _facts(media_required=True, media_content_verified=False)
    assert decide_verdict(facts) is ArtifactVerdict.CORRUPT_HASH


def test_media_content_mismatch_ignored_when_media_not_required() -> None:
    facts = _facts(media_required=False, media_content_verified=False)
    assert decide_verdict(facts) is ArtifactVerdict.REUSABLE


def test_missing_media_wins_over_descriptor_content_check() -> None:
    facts = _facts(media_required=True, media_exists=False, descriptor_content_verified=False)
    assert decide_verdict(facts) is ArtifactVerdict.MISSING


def test_profile_mismatch_is_version_mismatch() -> None:
    facts = _facts(profile_check_applicable=True, profile_id_valid=False)
    assert decide_verdict(facts) is ArtifactVerdict.VERSION_MISMATCH


def test_profile_check_not_applicable_ignores_profile_id_valid_flag() -> None:
    facts = _facts(profile_check_applicable=False, profile_id_valid=False)
    assert decide_verdict(facts) is ArtifactVerdict.REUSABLE


def test_content_corruption_wins_over_profile_mismatch() -> None:
    # 優先順位: MISSING > CORRUPT_SCHEMA > CORRUPT_HASH > VERSION_MISMATCH > REUSABLE
    facts = _facts(
        descriptor_content_verified=False, profile_check_applicable=True, profile_id_valid=False
    )
    assert decide_verdict(facts) is ArtifactVerdict.CORRUPT_HASH
