"""gate の検査入力: 技術 QA の分割と、upload と共有する説明欄の組み立て（ADR-0081 / INV-131）。

テストの理由は docs/testing/v3-phase3-quality.md。
"""

from __future__ import annotations

import uuid

import pytest

from contracts.artifacts import build_script_artifact, parse_script_artifact
from contracts.quality import QualityGate, QualityReason, QualityVerdict
from contracts.quality_records import CHECK_KEY_GATES, CheckKey
from contracts.render import TechnicalQaCheck, TechnicalQaReport
from contracts.upload import (
    UPLOAD_DESCRIPTION_COMPOSITION_ENV,
    build_youtube_metadata,
    upload_marker,
)
from domain.quality.records import validate_check_set
from domain.quality.technical_qa_checks import AUDIO_CHECK_NAMES, technical_qa_checks
from domain.render.qa import TECHNICAL_QA_CHECK_NAMES
from domain.upload.metadata_builder import build_upload_metadata

KEY = "e" * 64


def _qa(*, drop: str | None = None, extra: tuple[str, ...] = ()) -> TechnicalQaReport:
    names = [n for n in TECHNICAL_QA_CHECK_NAMES if n != drop] + list(extra)
    return TechnicalQaReport(
        passed=True,
        checks=tuple(TechnicalQaCheck(check=n, passed=True, detail="ok") for n in names),
    )


def _by_gate(report: TechnicalQaReport):
    return {c.gate: c for c in technical_qa_checks(report)}


def test_a_complete_technical_qa_yields_one_pass_per_gate_and_is_a_valid_set() -> None:
    checks = technical_qa_checks(_qa())
    validate_check_set(CheckKey.TECHNICAL_QA, checks)
    assert {c.gate for c in checks} == CHECK_KEY_GATES[CheckKey.TECHNICAL_QA]
    assert all(c.verdict is QualityVerdict.PASS and c.deterministic for c in checks)


@pytest.mark.parametrize("name", TECHNICAL_QA_CHECK_NAMES)
def test_dropping_any_required_check_blocks_its_own_gate_only(name: str) -> None:
    by_gate = _by_gate(_qa(drop=name))
    broken = QualityGate.AUDIO if name in AUDIO_CHECK_NAMES else QualityGate.VISUAL
    healthy = QualityGate.VISUAL if broken is QualityGate.AUDIO else QualityGate.AUDIO
    assert by_gate[broken].verdict is QualityVerdict.BLOCK
    assert by_gate[healthy].verdict is QualityVerdict.PASS


def test_a_hollow_report_with_one_check_is_blocked_on_both_gates() -> None:
    hollow = TechnicalQaReport(
        passed=True, checks=(TechnicalQaCheck(check="duration", passed=True, detail="ok"),)
    )
    assert {c.verdict for c in technical_qa_checks(hollow)} == {QualityVerdict.BLOCK}


def test_the_bgm_loudness_check_belongs_to_the_audio_gate() -> None:
    assert "bgm_loudness" in AUDIO_CHECK_NAMES
    ok = _by_gate(_qa(extra=("bgm_loudness",)))
    assert ok[QualityGate.AUDIO].verdict is QualityVerdict.PASS


def test_check_details_never_echo_stored_names() -> None:
    for check in technical_qa_checks(_qa(drop="audio_codec")):
        assert check.detail.startswith("technical qa")
        assert "audio_codec" not in check.detail


# --- 説明欄: upload と gate が同じ関数で組む ------------------------------------------------


def _script(narration: str = "A calm narration.", hook: str = "A hook"):
    return parse_script_artifact(
        build_script_artifact(
            episode_id="ep",
            language="en",
            title="A title",
            hook=hook,
            scenes=[
                {"id": f"s{n}", "narration": text, "visual": "v", "duration_ms": 8000}
                for n, text in enumerate([narration, "More narration.", "And the end."], start=1)
            ],
            metadata={"topic": "t", "generator": "g", "generator_model": "m"},
        )
    )


@pytest.mark.parametrize(
    "narration",
    ["A calm narration.", "日本語のナレーション。", "word " * 40],
    ids=["en", "ja", "long"],
)
def test_with_the_flag_off_the_builder_is_byte_identical_to_the_previous_call(
    monkeypatch: pytest.MonkeyPatch, narration: str
) -> None:
    monkeypatch.delenv(UPLOAD_DESCRIPTION_COMPOSITION_ENV, raising=False)
    script = _script(narration)
    before = build_youtube_metadata(
        upload_key=KEY,
        title=script.title,
        hook=script.hook,
        narration=script.narration,
        language=script.language,
    )
    assert build_upload_metadata(upload_key=KEY, script=script) == before


def test_with_the_flag_on_the_description_is_composed_from_the_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(UPLOAD_DESCRIPTION_COMPOSITION_ENV, "1")
    script = _script()
    metadata = build_upload_metadata(
        upload_key=KEY, script=script, credits=("Music: Someone (CC-BY 4.0)",)
    )
    assert metadata.description.startswith(script.hook)
    assert "Music: Someone (CC-BY 4.0)" in metadata.description
    assert upload_marker(KEY) in metadata.tags


def test_the_same_inputs_give_the_same_text_for_upload_and_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(UPLOAD_DESCRIPTION_COMPOSITION_ENV, "1")
    script = _script()
    assert build_upload_metadata(upload_key=KEY, script=script) == build_upload_metadata(
        upload_key=KEY, script=script
    )


def test_an_internal_id_in_the_narration_blocks_the_composed_description(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contracts.upload import DescriptionBlockedError

    monkeypatch.setenv(UPLOAD_DESCRIPTION_COMPOSITION_ENV, "1")
    script = _script(hook=f"see {uuid.uuid4()}")
    with pytest.raises(DescriptionBlockedError):
        build_upload_metadata(upload_key=KEY, script=script)


def test_reasons_used_for_technical_qa_are_the_gate_defects() -> None:
    blocked = technical_qa_checks(_qa(drop="audio_codec"))
    assert {c.reason_code for c in blocked} == {QualityReason.OK, QualityReason.AUDIO_DEFECT}
