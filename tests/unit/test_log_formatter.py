"""JSON 整形器の形式・必須フィールド・型・上限・切り詰めの事実（log-contract §1・§2・§4・§7）。

理由は docs/testing/logging-rationale.md。
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import pytest
from temporalio.exceptions import ApplicationError

from contracts.log_contract import (
    ATTRIBUTES_MAX_BYTES,
    EVENT_MAX_BYTES,
    EXCEPTION_STACK_MAX_BYTES,
    KEYWORD_MAX_CHARS,
    LOG_FIELDS,
    LOG_SCHEMA_VERSION,
    MESSAGE_MAX_BYTES,
    REQUIRED_APP_FIELDS,
    RESPONSE_EXCERPT_MAX_BYTES,
    EventName,
    FieldType,
)
from infrastructure.logging.context import log_context
from infrastructure.logging.formatter import (
    JsonFormatter,
    ServiceIdentity,
    format_exception_chain,
    workflow_event_id,
)

IDENTITY = ServiceIdentity("production-image-worker", "test", "abc123")
TYPES = {f.name: f.type for f in LOG_FIELDS}


def _record(
    msg: str = "hello %s",
    args: tuple[Any, ...] = ("world",),
    *,
    level: int = logging.INFO,
    name: str = "tests.logger",
    avp: dict[str, Any] | None = None,
    exc: BaseException | None = None,
    **extra: Any,
) -> logging.LogRecord:
    exc_info = (type(exc), exc, exc.__traceback__) if exc is not None else None
    record = logging.LogRecord(name, level, __file__, 1, msg, args, exc_info)
    if avp is not None:
        record.avp = avp
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def _format(record: logging.LogRecord, formatter: JsonFormatter | None = None) -> dict[str, Any]:
    line = (formatter or JsonFormatter(IDENTITY)).format(record)
    assert "\n" not in line
    assert len(line.encode("utf-8")) <= EVENT_MAX_BYTES
    return json.loads(line)


def _assert_types(event: dict[str, Any]) -> None:
    """全キーが契約のフィールドで、型が mapping 型に合う（B の mapping 生成の前提）。"""
    for key, value in event.items():
        assert key in TYPES, key
        ftype = TYPES[key]
        if ftype is FieldType.KEYWORD:
            values = value if isinstance(value, list) else [value]
            assert all(isinstance(v, str) and len(v) <= KEYWORD_MAX_CHARS for v in values), key
        elif ftype in (FieldType.INTEGER, FieldType.LONG):
            assert isinstance(value, int) and not isinstance(value, bool), key
        elif ftype is FieldType.DOUBLE:
            assert isinstance(value, float), key
        elif ftype is FieldType.BOOLEAN:
            assert isinstance(value, bool), key
        elif ftype is FieldType.TEXT or ftype is FieldType.DATE:
            assert isinstance(value, str), key
        elif ftype is FieldType.OPAQUE_OBJECT:
            assert isinstance(value, dict), key


def test_plain_record_has_every_required_field() -> None:
    event = _format(_record())
    assert set(REQUIRED_APP_FIELDS) <= set(event)
    assert event["schema_version"] == LOG_SCHEMA_VERSION
    assert event["event_name"] == EventName.LOG_RECORD.value
    assert event["message"] == "hello world"
    assert event["level"] == "INFO"
    assert event["service_name"] == "production-image-worker"
    assert event["environment"] == "test"
    assert event["git_sha"] == "abc123"
    assert event["logger"] == "tests.logger"
    assert event["@timestamp"].endswith("Z") and len(event["@timestamp"]) == 24
    uuid.UUID(hex=event["event_id"])
    _assert_types(event)


def test_unknown_identity_values_are_marked_unknown() -> None:
    identity = ServiceIdentity.from_env({"AVP_ENVIRONMENT": "staging"})
    event = _format(_record(), JsonFormatter(identity))
    assert (event["service_name"], event["environment"], event["git_sha"]) == (
        "unknown",
        "unknown",
        "unknown",
    )


def test_event_fields_come_from_the_single_avp_extra_key() -> None:
    event = _format(
        _record(
            avp={
                "event_name": EventName.RESERVATION_RESERVED.value,
                "episode_id": "ep-1",
                "scene_id": "sb6",
                "provider_attempt": 2,
                "duration_ms": 12,
                "retryable": True,
                "error_code": ["content_policy_violation", "file_download_error"],
                "free_form": {"nested": 1},
                "ingested_at": "2026-01-01T00:00:00Z",
            }
        )
    )
    assert event["event_name"] == "reservation.reserved"
    assert event["episode_id"] == "ep-1"
    assert event["provider_attempt"] == 2
    assert event["duration_ms"] == 12.0
    assert event["retryable"] is True
    assert event["error_code"] == ["content_policy_violation", "file_download_error"]
    # 契約に無いキー・Collector が書くフィールドは attributes へ（トップレベルに出さない）
    assert event["attributes"]["free_form"] == {"nested": 1}
    assert event["attributes"]["ingested_at"] == "2026-01-01T00:00:00Z"
    assert "ingested_at" not in event
    _assert_types(event)


def test_values_of_the_wrong_type_are_moved_to_attributes() -> None:
    event = _format(_record(avp={"http_status": "not-a-number", "retryable": "yes"}))
    assert "http_status" not in event and "retryable" not in event
    assert event["attributes"]["http_status"] == "not-a-number"
    _assert_types(event)


def test_emitter_cannot_override_the_fields_the_formatter_owns() -> None:
    event = _format(_record(avp={"service_name": "spoofed", "event_id": "x"}))
    assert event["service_name"] == "production-image-worker"
    assert event["event_id"] != "x"


def test_context_is_attached_and_the_emitter_wins() -> None:
    with log_context(episode_id="ep-ctx", scene_id="sb1"):
        event = _format(_record(avp={"scene_id": "sb2"}))
    assert event["episode_id"] == "ep-ctx"
    assert event["scene_id"] == "sb2"
    assert "episode_id" not in _format(_record())


def test_temporal_activity_info_is_mapped_to_contract_names() -> None:
    event = _format(
        _record(
            temporal_activity={
                "activity_id": "3",
                "activity_type": "production.image.submit",
                "attempt": 2,
                "namespace": "default",
                "task_queue": "production-image",
                "workflow_id": "production-ep",
                "workflow_run_id": "run-1",
                "workflow_type": "ProductionWorkflow",
            }
        )
    )
    assert event["activity_attempt"] == 2
    assert event["run_id"] == "run-1"
    assert event["activity_type"] == "production.image.submit"
    _assert_types(event)


def test_keyword_values_are_cut_at_the_mapping_limit() -> None:
    # 長い英数字の連続は base64 様として伏せられるので、区切りのある値で試す
    event = _format(_record(avp={"scene_id": "sb-6 " * (KEYWORD_MAX_CHARS // 5 + 20)}))
    assert len(event["scene_id"]) == KEYWORD_MAX_CHARS
    assert event["truncated"] is True


def test_message_is_cut_by_utf8_bytes() -> None:
    event = _format(_record("あ" * MESSAGE_MAX_BYTES, ()))
    assert len(event["message"].encode("utf-8")) <= MESSAGE_MAX_BYTES
    assert event["truncated"] is True
    assert "あ" in event["message"]  # ensure_ascii=False（文字化けしない）


def test_response_excerpt_is_json_and_its_cut_is_flagged() -> None:
    event = _format(_record(avp={"response_excerpt": {"types": ["x" * 10], "n": 1}}))
    assert json.loads(event["response_excerpt"]) == {"types": ["xxxxxxxxxx"], "n": 1}
    big = _format(_record(avp={"response_excerpt": "y " * RESPONSE_EXCERPT_MAX_BYTES}))
    assert len(big["response_excerpt"].encode("utf-8")) <= RESPONSE_EXCERPT_MAX_BYTES
    assert big["response_truncated"] is True


def test_attributes_are_bounded() -> None:
    event = _format(_record(avp={"a": "x " * 1500, "b": "y " * 1500, "c": 1}))
    attrs = event["attributes"]
    assert len(json.dumps(attrs, ensure_ascii=False).encode("utf-8")) <= ATTRIBUTES_MAX_BYTES
    assert attrs["c"] == 1
    assert event["truncated"] is True


def test_the_whole_line_never_exceeds_the_event_limit() -> None:
    avp = {
        "response_excerpt": "r " * 2000,
        "error_message": "e " * 500,
        **{f"k{i}": "v " * 200 for i in range(10)},
    }
    with pytest.raises(RuntimeError) as caught:
        raise RuntimeError("m " * 1500)
    err = caught.value
    event = _format(_record("z " * 2500, (), avp=avp, exc=err))
    assert event["truncated"] is True
    assert set(REQUIRED_APP_FIELDS) <= set(event)


def _deep(n: int) -> None:
    if n == 0:
        raise ValueError("leaf failure")
    _deep(n - 1)


def test_stack_keeps_the_root_cause_of_a_long_chain() -> None:
    """全体の末尾だけを残すと根本原因が消える（log-contract §7.7）。"""
    with pytest.raises(RuntimeError) as caught:
        try:
            _deep(200)
        except ValueError as root:
            raise RuntimeError("wrapper " + "w " * 10000) from root
    err = caught.value
    text, cut = format_exception_chain(err)
    assert cut is True
    assert len(text.encode("utf-8")) <= EXCEPTION_STACK_MAX_BYTES
    assert "ValueError: leaf failure" in text
    assert "RuntimeError: wrapper" in text

    event = _format(_record("failed", (), exc=err))
    assert event["stack_truncated"] is True
    assert event["error_type"] == "RuntimeError"
    assert "ValueError: leaf failure" in event["exception_stack"]


def _ping(n: int) -> None:
    if n == 0:
        raise ValueError("leaf failure")
    _pong(n - 1)


def _pong(n: int) -> None:
    _ping(n)


def test_deep_stacks_keep_the_first_and_last_frames() -> None:
    # 交互の再帰（同じ行の連続は traceback が畳むので、別々の frame を作る）
    with pytest.raises(ValueError) as caught:
        _ping(20)
    text, cut = format_exception_chain(caught.value)
    assert cut is False
    assert "frame(s) omitted" in text
    assert "test_deep_stacks_keep_the_first_and_last_frames" in text  # 先頭
    assert 'raise ValueError("leaf failure")' in text  # 末尾


def test_exception_groups_and_notes_are_kept() -> None:
    inner = KeyError("missing")
    inner.add_note("note: scene sb6")
    group = ExceptionGroup("several", [inner, ValueError("bad")])
    with pytest.raises(ExceptionGroup) as caught:
        raise group
    text, _ = format_exception_chain(caught.value)
    assert "KeyError" in text and "ValueError: bad" in text
    assert "note: scene sb6" in text


def test_application_error_type_is_the_domain_name() -> None:
    with pytest.raises(ApplicationError) as caught:
        raise ApplicationError("ProviderRejectedError: nope", type="ProviderRejectedError")
    event = _format(_record("failed", (), exc=caught.value))
    assert event["error_type"] == "ProviderRejectedError"


def test_a_broken_record_falls_back_to_the_fixed_minimal_form() -> None:
    record = _record("%d", ("not-an-int",))
    event = _format(record)
    assert event["message"] == "log formatting failed"
    assert event["event_name"] == "log.record"
    assert event["error_type"] == "TypeError"
    assert set(REQUIRED_APP_FIELDS) <= set(event)


def test_levels_map_to_the_contract_vocabulary() -> None:
    assert _format(_record(level=5))["level"] == "DEBUG"
    assert _format(_record(level=logging.WARNING))["level"] == "WARNING"
    assert _format(_record(level=logging.CRITICAL))["level"] == "CRITICAL"


# ---------------------------------------------------------------- Workflow の event_id（§4）


def test_workflow_event_id_is_deterministic_and_every_input_matters() -> None:
    base = ("wf", "run", 18, 0, "stage.started")
    first = workflow_event_id(*base)
    assert first == workflow_event_id(*base)
    variants = [
        ("wf2", "run", 18, 0, "stage.started"),
        ("wf", "run2", 18, 0, "stage.started"),
        ("wf", "run", 19, 0, "stage.started"),
        ("wf", "run", 18, 1, "stage.started"),
        ("wf", "run", 18, 0, "stage.failed"),
    ]
    ids = {first, *(workflow_event_id(*v) for v in variants)}
    assert len(ids) == len(variants) + 1


def test_workflow_event_id_does_not_collide_across_a_grid() -> None:
    ids = {
        workflow_event_id(f"wf{w}", f"run{r}", h, s, e)
        for w in range(3)
        for r in range(3)
        for h in range(3, 30, 3)
        for s in range(4)
        for e in ("stage.started", "stage.succeeded", "log.record")
    }
    assert len(ids) == 3 * 3 * 9 * 4 * 3


def test_outside_a_workflow_the_id_is_random_even_with_workflow_info() -> None:
    """Workflow スレッドの外（history_length が取れない）では uuid4 に倒す。"""
    info = {"workflow_id": "wf", "run_id": "run"}
    a = _format(_record(temporal_workflow=info))
    b = _format(_record(temporal_workflow=info))
    assert a["event_id"] != b["event_id"]
    assert a["workflow_id"] == "wf" and a["run_id"] == "run"


@pytest.mark.parametrize("n", [1, 3])
def test_seq_counts_per_run_and_history_length(n: int) -> None:
    from infrastructure.logging.formatter import _WorkflowSeq

    seq = _WorkflowSeq(size=2)
    assert [seq.next("r", 3) for _ in range(n)] == list(range(n))
    assert seq.next("r", 5) == 0
    assert seq.next("r2", 3) == 0
    # 上限を超えたら古いものから忘れる（eviction で消えてよい）
    assert seq.next("r", 3) == 0
