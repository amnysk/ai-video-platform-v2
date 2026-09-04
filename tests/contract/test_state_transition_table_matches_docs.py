"""設計書の遷移表と実装の表を**行単位で**突き合わせる。

`docs/domain/state-transitions.md` は自らを「唯一の権威」と名乗り、
「表の全行を網羅するテストがある」と書いている。しかしその照合を行う機械が
存在しなかった（設計書だけが主張していた状態）。AGENTS.md が名指しで禁じる
「テストの無い契約」に当たるため、ここで閉じる。

生成側（設計書）と取り込み側（コード）を**同じテストの中で**比較する。
片側だけのアサートは、このプロジェクトの事故を1件も捕まえていない。
"""

from __future__ import annotations

import pathlib
import re

from contracts.states import EPISODE_ACTIVE_STATUSES, EpisodeStatus, JobStatus
from domain.episode.transitions import EPISODE_TRANSITIONS
from domain.job.transitions import JOB_TRANSITIONS

REPO = pathlib.Path(__file__).resolve().parents[2]
STATE_DOC = REPO / "docs" / "domain" / "state-transitions.md"

#: 表の中で状態名ではない左辺。意味を持つのでコード側から展開して照合する。
_INITIAL = "（なし）"
_ANY_ACTIVE = "任意の非terminal"


def _doc_rows() -> list[tuple[str, str]]:
    """遷移表の (From, To) を読む。`|---|` 区切りとヘッダは除く。"""
    text = STATE_DOC.read_text(encoding="utf-8")
    table = re.search(r"^\| From \| To \|.*?(?=\n\n)", text, re.DOTALL | re.MULTILINE)
    assert table, "state-transitions.md に遷移表が見つからない"

    rows: list[tuple[str, str]] = []
    for line in table.group(0).splitlines()[2:]:
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        rows.append((cells[0].strip("`"), cells[1].strip("`")))
    return rows


def _documented_pairs() -> set[tuple[EpisodeStatus, EpisodeStatus]]:
    pairs: set[tuple[EpisodeStatus, EpisodeStatus]] = set()
    for source, target in _doc_rows():
        if source == _INITIAL:
            continue  # 生成であって遷移ではない
        if source == _ANY_ACTIVE:
            pairs.update((s, EpisodeStatus(target)) for s in EPISODE_ACTIVE_STATUSES)
            continue
        pairs.add((EpisodeStatus(source), EpisodeStatus(target)))
    return pairs


def _implemented_pairs() -> set[tuple[EpisodeStatus, EpisodeStatus]]:
    return {(source, target) for (source, _event), target in EPISODE_TRANSITIONS.items()}


def test_documented_transitions_all_exist_in_code() -> None:
    missing = sorted(
        f"{s.value} -> {t.value}" for s, t in _documented_pairs() - _implemented_pairs()
    )
    assert not missing, (
        "設計書にあるがコードに無い遷移: "
        f"{missing}. state-transitions.md と EPISODE_TRANSITIONS を一致させること"
    )


def test_implemented_transitions_are_all_documented() -> None:
    undocumented = sorted(
        f"{s.value} -> {t.value}" for s, t in _implemented_pairs() - _documented_pairs()
    )
    assert not undocumented, (
        "コードにあるが設計書に無い遷移: "
        f"{undocumented}. 挙動を変えたら設計書を同じコミットで更新すること（AGENTS.md §7）"
    )


def test_every_documented_status_name_is_a_real_status() -> None:
    """表に綴り間違いの状態名が紛れていないこと。"""
    for source, target in _doc_rows():
        if source not in {_INITIAL, _ANY_ACTIVE}:
            EpisodeStatus(source)
        EpisodeStatus(target)


def test_doc_and_code_agree_on_terminal_statuses() -> None:
    text = STATE_DOC.read_text(encoding="utf-8")
    section = re.search(r"## terminal状態\n\n(.+?)。", text, re.DOTALL)
    assert section, "terminal状態の節が見つからない"
    documented = {s.strip("`") for s in re.findall(r"`(\w+)`", section.group(1))}

    from contracts.states import EPISODE_TERMINAL_STATUSES

    assert documented == {s.value for s in EPISODE_TERMINAL_STATUSES}


def test_job_status_vocabulary_matches_the_docs() -> None:
    """docs/domain/job.md の status 語彙と JobStatus が一致すること。"""
    job_doc = (REPO / "docs" / "domain" / "job.md").read_text(encoding="utf-8")
    line = re.search(r"- `status`: (.+?)（ADR-0006）", job_doc, re.DOTALL)
    assert line, "job.md に status 語彙の行が見つからない"
    documented = set(re.findall(r"`(\w+)`", line.group(1)))
    assert documented == {s.value for s in JobStatus}


def test_job_transition_sources_and_targets_are_known_statuses() -> None:
    for (source, _event), target in JOB_TRANSITIONS.items():
        assert isinstance(source, JobStatus)
        assert isinstance(target, JobStatus)
