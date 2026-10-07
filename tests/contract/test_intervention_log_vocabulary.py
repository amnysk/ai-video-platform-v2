"""判断・介入のイベントと相関項目がログ契約の語彙にあること（ADR-0041 §7 / 設計 §15）。

``episode_events.event_name``（``contracts.intervention.InterventionEvent``）は
ログの ``EventName`` と**同じ文字列**で投影する。片方にだけ足すと、投影が
``log.record`` に落ちるか、発行時に失敗する。
理由は docs/testing/intervention-d-rationale.md §1。
"""

from __future__ import annotations

from pathlib import Path

from contracts.intervention import ActorType, InterventionEvent
from contracts.log_contract import LOG_FIELDS, EventName, FieldType

REPO = Path(__file__).resolve().parents[2]
_TYPES = {f.name: f.type for f in LOG_FIELDS}


def test_every_intervention_event_is_a_log_event_name() -> None:
    log_values = {e.value for e in EventName}
    missing = [e.value for e in InterventionEvent if e.value not in log_values]
    assert not missing, f"EventName に無い InterventionEvent: {missing}"


def test_intervention_event_members_have_the_same_python_names() -> None:
    """``EventName[member.name]`` で引けること（投影の対応表がメンバー名で書ける）。"""
    for event in InterventionEvent:
        assert EventName[event.name].value == event.value


def test_correlation_fields_of_the_design_are_searchable_fields() -> None:
    """設計 §15 の相関項目・agent/tool 共通項目。既存項目へ写したものは写し先が在ること。"""
    keyword = {
        "manifest_id",
        "agent_run_id",
        "decision_id",
        "command_id",
        "actor_type",
        "model",
        "prompt_version",
        "correlation_id",
        # 既存へ写したもの（log-contract §3）
        "artifact_id",  # artifact_revision（artifact_metadata の1行 = 1 revision）
        "reservation_id",  # tool_call_id（台帳の呼び出し）
        "research_call_id",  # tool_call_id（Research の呼び出し）
        "provider",  # tool_name
        "workflow_id",
        "run_id",
        "trace_id",
        "outcome",
    }
    for name in keyword:
        assert _TYPES.get(name) is FieldType.KEYWORD, name
    for name in ("usage_input", "usage_output", "event_seq"):
        assert _TYPES.get(name) is FieldType.LONG, name
    for name in ("estimated_cost_usd", "actual_cost_usd", "duration_ms"):
        assert _TYPES.get(name) is FieldType.DOUBLE, name


def test_new_field_names_do_not_look_like_secrets() -> None:
    """``token`` を含むキーは安全化で値が伏せられる（log-contract §7.2）。件数を伏せさせない。"""
    from infrastructure.logging.redaction import is_secret_key

    for name in ("usage_input", "usage_output", "estimated_cost_usd", "actual_cost_usd"):
        assert not is_secret_key(name), name


def test_actor_types_are_documented() -> None:
    doc = (REPO / "docs" / "observability" / "log-contract.md").read_text(encoding="utf-8")
    missing = [a.value for a in ActorType if f"`{a.value}`" not in doc]
    assert not missing, missing
