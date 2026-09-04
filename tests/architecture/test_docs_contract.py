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


def test_every_adr_is_listed_in_the_index() -> None:
    """ADRを書いたのに README の一覧へ足し忘れる、という片側更新を防ぐ。"""
    index = (REPO / "docs" / "decisions" / "README.md").read_text(encoding="utf-8")
    missing = [
        adr.name
        for adr in sorted((REPO / "docs" / "decisions").glob("[0-9]*.md"))
        if adr.name not in index
    ]
    assert not missing, f"docs/decisions/README.md の一覧に載っていないADR: {missing}"


def test_every_test_referenced_by_the_docs_actually_exists() -> None:
    """設計書が名指ししたテストが実在すること。

    「設計書に書いてある != 実装されている」を機械で止める（AGENTS.md）。
    docs/invariants.md の「機械検査」欄のように、テスト名を書いた文書は多いが、
    その名前が腐っていないことを保証する機械は無かった。
    """
    reference = re.compile(r"tests/[\w/]+\.py(?:::(\w+))?")
    problems: list[str] = []
    # 旧repo(ai-video-pipeline)のファイルを棚卸しする文書。ここの tests/ は他repoのもの。
    foreign = {REPO / "docs" / "operations" / "legacy-asset-inventory.md"}

    docs = sorted(REPO.glob("docs/**/*.md")) + [REPO / "AGENTS.md", REPO / "README.md"]
    for doc in (d for d in docs if d not in foreign):
        text = doc.read_text(encoding="utf-8")
        for match in reference.finditer(text):
            path = REPO / match.group(0).split("::")[0]
            if not path.exists():
                problems.append(f"{doc.relative_to(REPO)}: missing file {match.group(0)}")
                continue
            function = match.group(1)
            if function and f"def {function}" not in path.read_text(encoding="utf-8"):
                problems.append(f"{doc.relative_to(REPO)}: missing test {match.group(0)}")

    assert not problems, "\n".join(problems)
