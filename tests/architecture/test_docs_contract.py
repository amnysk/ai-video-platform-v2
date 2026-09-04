"""ドキュメント側の契約検査。

AGENTS.md が「番号を再利用しない」「機械検査欄を持つ」と定めた不変条件の書式を、
人手のレビューではなく機械で保つ。
"""

from __future__ import annotations

import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[2]
INVARIANTS = REPO / "docs" / "invariants.md"

REQUIRED_DOCS = [
    "AGENTS.md",
    "README.md",
    "docs/invariants.md",
    "docs/failure-policy.md",
    "docs/architecture/overview.md",
    "docs/architecture/components.md",
    "docs/architecture/data-flow.md",
    "docs/domain/episode.md",
    "docs/domain/job.md",
    "docs/domain/artifact.md",
    "docs/domain/state-transitions.md",
    "docs/decisions/README.md",
]

ADR_SECTIONS = ["## Status", "## Context", "## Decision", "## Alternatives", "## Consequences"]


def test_required_documents_exist() -> None:
    missing = [p for p in REQUIRED_DOCS if not (REPO / p).exists()]
    assert not missing, f"missing required docs: {missing}"


def test_invariant_ids_are_unique_and_sequential() -> None:
    ids = re.findall(r"^### (INV-\d+)\b", INVARIANTS.read_text(encoding="utf-8"), re.MULTILINE)
    assert ids, "no invariants found"
    assert len(ids) == len(set(ids)), "duplicate invariant id (番号は再利用しない)"
    numbers = [int(i.removeprefix("INV-")) for i in ids]
    assert numbers == sorted(numbers), "invariant ids must appear in ascending order"


def test_every_invariant_declares_a_machine_check() -> None:
    """「機械検査」欄の無い不変条件を作らない。未実装なら `未検査` と明記する。"""
    text = INVARIANTS.read_text(encoding="utf-8")
    blocks = re.split(r"^### (INV-\d+)", text, flags=re.MULTILINE)[1:]
    missing = [blocks[i] for i in range(0, len(blocks), 2) if "**機械検査**" not in blocks[i + 1]]
    assert not missing, f"invariants without a 機械検査 line: {missing}"


def test_adrs_have_all_required_sections() -> None:
    adrs = sorted((REPO / "docs" / "decisions").glob("[0-9]*.md"))
    assert adrs, "no ADRs found"
    problems: list[str] = []
    for adr in adrs:
        text = adr.read_text(encoding="utf-8")
        for section in ADR_SECTIONS:
            if section not in text:
                problems.append(f"{adr.name}: missing {section}")
    assert not problems, "\n".join(problems)


def test_adr_numbers_are_unique() -> None:
    numbers = [p.name.split("-")[0] for p in (REPO / "docs" / "decisions").glob("[0-9]*.md")]
    assert len(numbers) == len(set(numbers)), "duplicate ADR number (番号は再利用しない)"
