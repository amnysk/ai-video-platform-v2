"""共有 Verdict の契約（INV-105）。理由は docs/testing/verdict-rationale.md。"""

from __future__ import annotations

import itertools

import pytest

from contracts.geo import GEO_VERDICT_ORDER, GeoCheck, GeoVerdict, GeoVerification
from contracts.geo import stricter as geo_stricter
from contracts.quality import VERDICT_SEVERITY, QualityVerdict
from contracts.verdict import VERDICT_ORDER, Verdict, stricter, worst


def test_the_legacy_names_are_the_one_shared_enum() -> None:
    assert GeoVerdict is Verdict
    assert QualityVerdict is Verdict


def test_wire_values_are_unchanged() -> None:
    assert [v.value for v in Verdict] == ["pass", "warn", "review", "block"]
    assert GeoVerdict("block") is Verdict.BLOCK
    assert QualityVerdict.WARN == "warn"


def test_severity_is_defined_once_and_ordered() -> None:
    assert VERDICT_ORDER == (Verdict.PASS, Verdict.WARN, Verdict.REVIEW, Verdict.BLOCK)
    assert GEO_VERDICT_ORDER is VERDICT_ORDER
    assert [VERDICT_SEVERITY[v] for v in VERDICT_ORDER] == [0, 1, 2, 3]
    assert geo_stricter is stricter


@pytest.mark.parametrize(("a", "b"), list(itertools.product(Verdict, Verdict)))
def test_stricter_is_the_max_and_symmetric(a: Verdict, b: Verdict) -> None:
    assert stricter(a, b) is stricter(b, a)
    assert stricter(a, b) in (a, b)
    assert VERDICT_SEVERITY[stricter(a, b)] == max(VERDICT_SEVERITY[a], VERDICT_SEVERITY[b])


def test_worst_of_nothing_is_pass_and_block_dominates() -> None:
    assert worst([]) is Verdict.PASS
    assert worst([Verdict.WARN, Verdict.BLOCK, Verdict.REVIEW]) is Verdict.BLOCK


def test_existing_models_serialize_identically() -> None:
    v = GeoVerification(
        verdict=Verdict.WARN, checks=(GeoCheck(name="x", outcome=Verdict.WARN, detail="d"),)
    )
    dumped = v.model_dump(mode="json")
    assert dumped["verdict"] == "warn"
    assert dumped["checks"][0]["outcome"] == "warn"
    assert GeoVerification.model_validate_json(v.model_dump_json()) == v
