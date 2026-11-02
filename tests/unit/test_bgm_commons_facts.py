"""Commons API 応答 → LicenseFacts（INV-150 の入力）。実在の応答 5 件と、HTML 除去の決定性。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from contracts.music_sources import MusicSourceId
from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from domain.music.commons_facts import (
    build_evidence_payload,
    pages_from_response,
    parse_commons_evidence,
    strip_html,
)

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "bgm_commons"
FETCHED = "2026-11-02T00:00:00+00:00"


def _page(name: str) -> dict:
    return pages_from_response(json.loads((FIX / f"{name}.json").read_text("utf-8")))[0]


def _parse(name: str):
    return parse_commons_evidence(build_evidence_payload(_page(name), fetched_at=FETCHED))


def test_ck61_is_cc0_own_work_with_machine_facts() -> None:
    info = _parse("ck61")
    f = info.facts
    assert f.source is MusicSourceId.WIKIMEDIA_COMMONS
    assert f.license_short_name == "CC0"
    assert f.license_code == "cc0"
    assert f.credit == "Own work"
    assert f.artist == "Wilfredor"
    assert f.attribution_required is False
    assert f.restrictions == ""
    assert f.deletion_or_dispute is False
    assert f.original_file_url and f.original_file_url.startswith("https://upload.wikimedia.org/")
    assert f.source_page_url == "https://commons.wikimedia.org/wiki/File:Ambient_music_test,_Yamaha_CK61.flac"
    assert info.sha1 == "fbe9e5fbe4b727fac1608dec68c94a956d9cbd65"
    assert info.size_bytes == 10185435
    assert info.mime == "audio/flac"
    assert info.duration_ms == 197754


def test_evidence_sha256_is_the_canonical_json_hash_of_the_stored_payload() -> None:
    payload = build_evidence_payload(_page("ck61"), fetched_at=FETCHED)
    info = parse_commons_evidence(payload)
    assert info.facts.evidence_sha256 == sha256_hex(canonical_json_bytes(payload))
    # 取得時刻が違えば別の証拠
    other = build_evidence_payload(_page("ck61"), fetched_at="2026-11-03T00:00:00+00:00")
    assert parse_commons_evidence(other).facts.evidence_sha256 != info.facts.evidence_sha256


def test_other_fixtures_are_parsed_as_what_they_are() -> None:
    eastern = _parse("eastern").facts
    assert eastern.license_code == "cc-by-3.0" and eastern.attribution_required is True
    assert eastern.artist == "Kevin MacLeod"
    shika = _parse("shika").facts
    assert shika.license_code == "pd" and shika.license_short_name == "Public domain"
    assert shika.credit and "Own work" not in shika.credit
    assert _parse("kuniholm").facts.credit == "Own work"
    assert _parse("piano051").facts.license_code == "cc0"


def test_strip_html_is_deterministic_and_removes_markup() -> None:
    raw = '<bdi><a href="x">Wil&amp;fredor</a></bdi>  <span>\n second</span>'
    assert strip_html(raw) == "Wil&fredor second"
    assert strip_html(raw) == strip_html(raw)
    assert strip_html("") == ""
    assert strip_html(None) is None


@pytest.mark.parametrize(
    "raw",
    [
        "Own work<script>alert(1)</script>",
        "Own work<!-- actually copied from elsewhere -->",
        "<style>x{}</style>Own work",
        'Own work<iframe src="//evil"></iframe>',
    ],
)
def test_dangerous_markup_never_collapses_to_a_clean_own_work(raw: str) -> None:
    assert strip_html(raw) != "Own work"


def test_deletion_and_dispute_signals_from_categories_and_templates() -> None:
    page = _page("ck61")
    page = {**page, "categories": [{"ns": 14, "title": "Category:Copyright violations"}]}
    assert parse_commons_evidence(build_evidence_payload(page, fetched_at=FETCHED)).facts.deletion_or_dispute
    page = {**_page("ck61"), "templates": [{"ns": 10, "title": "Template:Copyvio"}]}
    assert parse_commons_evidence(build_evidence_payload(page, fetched_at=FETCHED)).facts.deletion_or_dispute
    page = {**_page("ck61"), "templates": [{"ns": 10, "title": "Template:Delete"}]}
    assert parse_commons_evidence(build_evidence_payload(page, fetched_at=FETCHED)).facts.deletion_or_dispute
    # extmetadata の Categories にあっても検出する
    page = _page("ck61")
    page["imageinfo"][0]["extmetadata"]["Categories"]["value"] += "|Pages nominated for deletion"
    assert parse_commons_evidence(build_evidence_payload(page, fetched_at=FETCHED)).facts.deletion_or_dispute


def test_formatversion_2_list_pages_are_accepted() -> None:
    resp = {"query": {"pages": [_page("ck61")]}}
    assert len(pages_from_response(resp)) == 1
    assert pages_from_response({"batchcomplete": ""}) == []


def test_missing_imageinfo_means_no_info() -> None:
    page = {"pageid": 1, "ns": 6, "title": "File:x.ogg", "missing": ""}
    assert parse_commons_evidence(build_evidence_payload(page, fetched_at=FETCHED)).facts.license_short_name is None
