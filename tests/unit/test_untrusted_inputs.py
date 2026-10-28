"""INV-106: LLM 由来の文字列は検査結果・レポート・ログ用フィールドに残らない。

理由は docs/testing/verdict-rationale.md。敵対的な文字列（改行・偽 API key・長大）を地名・ラベル・
as_of・主張の本文に入れ、結果のどこにも現れないことを見る。
"""

from __future__ import annotations

import json

import pytest

from contracts.geo import GeoCheck, GeoReason, GeoVerdict
from contracts.log_contract import LOG_FIELD_NAMES
from contracts.quality import (
    QUALITY_REPORT_SCHEMA_VERSION,
    QualityCheck,
    QualityGate,
    QualityReason,
    QualityReportError,
    QualityVerdict,
)
from contracts.untrusted import is_safe_detail, untrusted_ref
from domain.geo import verify_manifest, verify_scene
from tests.support import fake_gazetteer as fg

SECRET = "sk-LEAKCANARY0123456789abcdef"
INJECT = "INJECTED-FAKE-LOG-LINE"
HOSTILE = f"{SECRET}\n{INJECT}\r\x1b[31m "
BIG = (HOSTILE + "A" * 100) * 100  # 約 10k 字


def _dump(result: object) -> str:
    return json.dumps(result.model_dump(mode="json"), ensure_ascii=False)  # type: ignore[attr-defined]


def _assert_clean(text: str) -> None:
    for needle in (SECRET, INJECT, "\x1b", "sk-"):
        assert needle not in text, needle


def _hostile_results() -> list[object]:
    out: list[object] = []
    # (1) 地名: 参照にない / 別の地点の名前 / 時代錯誤。display_name は 200 字まで
    for name in (HOSTILE[:200], f"Kyoto {SECRET}", "Fuchu"):
        m = fg.manifest(
            places=[fg.place(display_name=name)],
            labels=[{"place_id": "gz:sekigahara", "text": name}],
        )
        out.append(verify_manifest(m, fg.ALL_REFERENCES, fg.GOOD_CLAIMS))
    # (2) ラベルだけが敵対的
    m = fg.manifest(labels=[{"place_id": "gz:sekigahara", "text": HOSTILE[:200]}])
    out.append(verify_manifest(m, fg.ALL_REFERENCES, fg.GOOD_CLAIMS))
    # (3) as_of（32 字まで）
    m = fg.manifest(as_of=SECRET)
    out.append(verify_manifest(m, fg.ALL_REFERENCES, fg.GOOD_CLAIMS))
    # (4) 主張の本文が敵対的（契約の上限 300 字。10k 字は quality の detail 側で検査）
    claim = fg.claim("C-001", text=BIG[:300])
    out.append(verify_manifest(fg.manifest(), fg.ALL_REFERENCES, (claim, fg.claim("C-002"))))
    # (5) 模式図 / manifest 無し
    out.append(verify_scene(True, None, fg.ALL_REFERENCES, fg.GOOD_CLAIMS))
    return out


def test_hostile_strings_never_appear_in_a_geo_verification() -> None:
    for result in _hostile_results():
        _assert_clean(_dump(result))


def test_hostile_strings_never_appear_in_geo_log_fields() -> None:
    for result in _hostile_results():
        for check in result.checks:  # type: ignore[attr-defined]
            _assert_clean(json.dumps(check.log_fields(scene_id="s1"), ensure_ascii=False))


def test_the_hostile_value_is_traceable_only_by_length_and_hash() -> None:
    m = fg.manifest(as_of=SECRET)
    result = verify_manifest(m, fg.ALL_REFERENCES, fg.GOOD_CLAIMS)
    (as_of,) = [c for c in result.checks if c.name == "as_of"]
    assert as_of.reason is GeoReason.AS_OF_UNKNOWN
    assert as_of.value_ref == untrusted_ref(SECRET)
    assert SECRET not in (as_of.value_ref or "")


def test_every_check_the_verifier_makes_carries_a_reason_code() -> None:
    for result in _hostile_results() + [
        verify_manifest(fg.manifest(), fg.ALL_REFERENCES, fg.GOOD_CLAIMS)
    ]:
        for check in result.checks:  # type: ignore[attr-defined]
            assert check.reason is not None
            assert (check.outcome is GeoVerdict.PASS) == (check.reason is GeoReason.OK)


def test_log_fields_use_only_existing_log_contract_fields() -> None:
    geo = verify_manifest(fg.manifest(), fg.ALL_REFERENCES, fg.GOOD_CLAIMS).checks[0]
    quality = QualityCheck(
        gate=QualityGate.GEO,
        verdict=QualityVerdict.BLOCK,
        reason_code=QualityReason.GEO_MISMATCH,
        detail="x",
        deterministic=True,
        scene_id="s1",
        evidence_ref="C-001",
    )
    for fields in (geo.log_fields(scene_id="s1"), quality.log_fields()):
        assert set(fields) <= LOG_FIELD_NAMES
        assert fields["outcome"] in {v.value for v in GeoVerdict}


@pytest.mark.parametrize("detail", [HOSTILE, "line1\nline2", "日本語", "x" * 501, "tab\there"])
def test_a_detail_that_could_carry_input_is_refused(detail: str) -> None:
    assert not is_safe_detail(detail)
    with pytest.raises(ValueError):
        GeoCheck(name="x", outcome=GeoVerdict.BLOCK, detail=detail)
    with pytest.raises(ValueError):
        QualityCheck(
            gate=QualityGate.CRITIC,
            verdict=QualityVerdict.REVIEW,
            reason_code=QualityReason.CRITIC_CONCERN,
            detail=detail,
            deterministic=False,
        )


def test_a_hostile_report_is_rejected_without_echoing_it() -> None:
    from contracts.quality import parse_quality_report

    raw = {
        "schema_version": QUALITY_REPORT_SCHEMA_VERSION,
        "episode_id": "e",
        "final_video_sha256": "a" * 64,
        "input_hash": "h",
        "generated_at": "2026-10-28T00:00:00+00:00",
        "generator_version": "g",
        "reviewer_version": "r",
        "verdict": "review",
        "checks": [
            {
                "gate": "critic",
                "verdict": "review",
                "reason_code": "critic_concern",
                "detail": BIG,
                "deterministic": False,
            }
        ],
    }
    with pytest.raises(QualityReportError) as err:
        parse_quality_report(raw)
    _assert_clean(str(err.value))


@pytest.mark.parametrize("scene_id", ["s1\nINJECT", SECRET, "S1", "s123"])
def test_a_bad_scene_id_is_refused(scene_id: str) -> None:
    with pytest.raises(ValueError):
        QualityCheck(
            gate=QualityGate.GEO,
            verdict=QualityVerdict.BLOCK,
            reason_code=QualityReason.GEO_MISMATCH,
            detail="x",
            deterministic=True,
            scene_id=scene_id,
        )
