"""migration 0016 の凍結語彙と ``contracts.intervention`` の一致（ADR-0041 / AGENTS.md §8）。

migration は contracts を import せず literal で凍結する。値を足したのに新しい migration を書かない
（DB の CHECK が古いまま）事故をここで止める。既存表を変えないことも検査する。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from contracts.intervention import (
    REQUIRED_SCOPE,
    ActorType,
    AgentRole,
    AgentRunOutcome,
    AnnotationKind,
    ChangeKind,
    ChangeSetStatus,
    CommandStatus,
    CommandType,
    ControlState,
    InterventionEvent,
    InterventionMode,
    RejectReason,
    ReviewPoint,
    RevisionLinkKind,
    Scope,
)
from contracts.roles import ROLE_SPECS

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "infrastructure/db/migrations/versions/0016_intervention_foundation.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("m0016", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("constant", "enum_cls"),
    [
        ("EPISODE_CONTROLS_CONTROL_STATE_VALUES", ControlState),
        ("EPISODE_CONTROLS_MODE_VALUES", InterventionMode),
        ("WAITING_POINTS", ReviewPoint),
        ("ARTIFACT_MANIFESTS_ACTOR_TYPE_VALUES", ActorType),
        ("REVISION_LINKS_KIND_VALUES", RevisionLinkKind),
        ("AGENT_RUNS_ROLE_VALUES", AgentRole),
        ("AGENT_RUNS_OUTCOME_VALUES", AgentRunOutcome),
        ("ANNOTATIONS_KIND_VALUES", AnnotationKind),
        ("CHANGE_SETS_KIND_VALUES", ChangeKind),
        ("CHANGE_SETS_STATUS_VALUES", ChangeSetStatus),
        ("INTERVENTION_COMMANDS_TYPE_VALUES", CommandType),
        ("INTERVENTION_COMMANDS_STATUS_VALUES", CommandStatus),
        ("INTERVENTION_COMMANDS_GRANTED_SCOPE_VALUES", Scope),
        ("REJECT_REASONS", RejectReason),
        ("EPISODE_EVENTS_EVENT_NAME_VALUES", InterventionEvent),
    ],
)
def test_frozen_vocabulary_matches_the_contracts(constant: str, enum_cls) -> None:
    frozen = getattr(_load(), constant)
    # 0016 の凍結語彙。後続の migration が足した値（0017: resolve_annotation）は
    # test_migration_0017_resolve_annotation.py が検査する
    later = {CommandType.RESOLVE_ANNOTATION}
    assert tuple(frozen) == tuple(m.value for m in enum_cls if m not in later)


def test_the_migration_does_not_import_contracts() -> None:
    assert "contracts" not in MIGRATION.read_text(encoding="utf-8").split('"""', 2)[2]


def test_every_command_type_has_a_required_scope() -> None:
    assert set(REQUIRED_SCOPE) == set(CommandType)


def test_no_command_type_changes_budget_schedule_or_visibility() -> None:
    """INV-47: 予算上限・Schedule・公開設定を変える command 型を語彙に持たない。"""
    forbidden = ("budget", "schedule", "visibility", "privacy", "publish", "limit")
    for member in CommandType:
        assert not any(word in member.value for word in forbidden), member


def test_role_specs_cover_the_three_initial_roles_with_real_implementations() -> None:
    assert set(ROLE_SPECS) == set(AgentRole)
    for spec in ROLE_SPECS.values():
        assert spec.implemented_by, spec.role
        assert spec.outputs and spec.termination and spec.inputs
    assert ROLE_SPECS[AgentRole.REVIEWER].may_create_revisions is False
