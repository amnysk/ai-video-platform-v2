"""1記録＝1行 JSON の整形器（log-contract §1・§2・§4・§7 / INV-39・INV-40）。

入力は標準 logging の ``LogRecord``。発行側のフィールドは ``record.avp``
（``extra={"avp": {...}}``）、
文脈は ``contextvars``（``log_context``）、Temporal の info は SDK が付ける ``temporal_workflow`` /
``temporal_activity`` から取る。**安全化してから切り詰める**。整形に失敗しても例外を外へ出さず、
最小の固定形で出し直す（INV-38）。

Workflow の記録の ``event_id`` はここで決定的に導く（Workflow のコードは ID を作らない）:
``uuid5(WORKFLOW_EVENT_NAMESPACE,
f"{workflow_id}:{run_id}:{history_length}:{seq}:{event_name}")``。
``seq`` は ``(run_id, history_length)`` ごとの発行順で、上限つき LRU に持つ。replay 中の記録は
SDK が抑止するので、ここへは届かない。
"""

from __future__ import annotations

import json
import logging
import threading
import traceback
import uuid
from collections import OrderedDict
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from temporalio import workflow as _workflow

from contracts.log_contract import (
    ATTRIBUTES_MAX_BYTES,
    ERROR_MESSAGE_MAX_BYTES,
    EVENT_MAX_BYTES,
    EXCEPTION_STACK_MAX_BYTES,
    KEYWORD_MAX_CHARS,
    LOG_FIELDS,
    LOG_SCHEMA_VERSION,
    MESSAGE_MAX_BYTES,
    RECORD_EXTRA_KEY,
    RESPONSE_EXCERPT_MAX_BYTES,
    UNKNOWN,
    Environment,
    EventName,
    FieldOrigin,
    FieldType,
)
from infrastructure.logging.context import current_context
from infrastructure.logging.redaction import Redactor, sanitize_text

#: Workflow の event_id の名前空間（固定値。変えると同じ記録の ID が変わる）
WORKFLOW_EVENT_NAMESPACE = uuid.UUID("5b0f3d64-2d7e-5a43-9a51-6c0a4f1e40aa")
#: ``(run_id, history_length)`` ごとの seq を覚えておく数。溢れたら古いものから忘れる
WORKFLOW_SEQ_CACHE_SIZE = 4096
#: stack で例外ごとに残す frame の数（先頭・末尾）
STACK_HEAD_FRAMES = 3
STACK_TAIL_FRAMES = 6
#: 例外 chain を辿る上限（循環・異常に長い chain への保険）
MAX_CHAIN = 8

_FIELD_TYPES: dict[str, FieldType] = {f.name: f.type for f in LOG_FIELDS}
_COLLECTOR_FIELDS = frozenset(f.name for f in LOG_FIELDS if f.origin is FieldOrigin.COLLECTOR)
#: 整形器自身が決めるフィールド（発行側の値では上書きしない）
_OWN_FIELDS = frozenset(
    {
        "@timestamp",
        "schema_version",
        "event_id",
        "level",
        "service_name",
        "environment",
        "git_sha",
        "logger",
        "redaction_applied",
        "response_truncated",
        "stack_truncated",
        "truncated",
    }
)
_ENVIRONMENTS = frozenset(e.value for e in Environment)


def _level_name(levelno: int) -> str:
    if levelno >= logging.CRITICAL:
        return "CRITICAL"
    if levelno >= logging.ERROR:
        return "ERROR"
    if levelno >= logging.WARNING:
        return "WARNING"
    if levelno >= logging.INFO:
        return "INFO"
    return "DEBUG"


def _timestamp(created: float) -> str:
    return (
        datetime.fromtimestamp(created, UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def truncate_bytes(text: str, limit: int) -> tuple[str, bool]:
    """UTF-8 で ``limit`` bytes 以下に切る（文字の途中で切らない）。"""
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text, False
    marker = "…"
    cut = raw[: max(0, limit - len(marker.encode("utf-8")))].decode("utf-8", "ignore")
    return cut + marker, True


def _json_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))


def exception_type_name(exc: BaseException) -> str:
    """観測した型名。Temporal の ``ApplicationError`` はその ``type``（ドメインの例外名）。"""
    app_type = getattr(exc, "type", None)
    if type(exc).__name__ == "ApplicationError" and isinstance(app_type, str) and app_type:
        return app_type
    return type(exc).__name__


def _chain(exc: BaseException) -> list[BaseException]:
    """原因が先に並ぶ例外 chain（``__cause__`` / ``__context__``）。"""
    out: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(out) < MAX_CHAIN:
        out.append(current)
        seen.add(id(current))
        if current.__cause__ is not None:
            current = current.__cause__
        elif current.__suppress_context__:
            current = None
        else:
            current = current.__context__
    out.reverse()
    return out


def _format_one(exc: BaseException, indent: str = "") -> str:
    frames = traceback.extract_tb(exc.__traceback__)
    lines: list[str] = []
    formatted = traceback.format_list(frames)
    if len(formatted) > STACK_HEAD_FRAMES + STACK_TAIL_FRAMES:
        omitted = len(formatted) - STACK_HEAD_FRAMES - STACK_TAIL_FRAMES
        formatted = [
            *formatted[:STACK_HEAD_FRAMES],
            f"  ... {omitted} frame(s) omitted ...\n",
            *formatted[-STACK_TAIL_FRAMES:],
        ]
    lines.extend(formatted)
    header = "".join(traceback.format_exception_only(type(exc), exc)).rstrip("\n")
    lines.append(header + "\n")
    if isinstance(exc, BaseExceptionGroup):
        for i, sub in enumerate(exc.exceptions[:5], 1):
            lines.append(f"{indent}  +-- sub-exception {i}:\n")
            lines.append(_format_one(sub, indent + "  "))
    return "".join(f"{indent}{line}" if indent else line for line in lines)


def format_exception_chain(
    exc: BaseException, limit: int = EXCEPTION_STACK_MAX_BYTES
) -> tuple[str, bool]:
    """chain の各例外の型・メッセージと frame の先頭・末尾を残し、全体を ``limit`` 以下にする。

    全体の末尾だけを残すと根本原因（chain の先頭）が消えるので、例外ごとに予算を分ける
    （log-contract §7.7）。返す文字列は安全化前。
    """
    chain = _chain(exc)
    blocks = [_format_one(e) for e in chain]
    separator = "\n--- next exception in chain ---\n"
    whole = separator.join(blocks)
    if len(whole.encode("utf-8")) <= limit:
        return whole, False
    budget = max(256, (limit - len(separator) * len(blocks)) // max(1, len(blocks)))
    trimmed: list[str] = []
    for block in blocks:
        raw = block.encode("utf-8")
        if len(raw) <= budget:
            trimmed.append(block)
            continue
        head = raw[: budget // 3].decode("utf-8", "ignore")
        tail = raw[-(budget - budget // 3 - 16) :].decode("utf-8", "ignore")
        trimmed.append(f"{head}\n  [...]\n{tail}")
    text, _ = truncate_bytes(separator.join(trimmed), limit)
    return text, True


class _WorkflowSeq:
    """``(run_id, history_length)`` ごとの発行順。Workflow は複数スレッドで動くので lock する。"""

    def __init__(self, size: int = WORKFLOW_SEQ_CACHE_SIZE) -> None:
        self._size = size
        self._seen: OrderedDict[tuple[str, int], int] = OrderedDict()
        self._lock = threading.Lock()

    def next(self, run_id: str, history_length: int) -> int:
        key = (run_id, history_length)
        with self._lock:
            seq = self._seen.pop(key, 0)
            self._seen[key] = seq + 1
            while len(self._seen) > self._size:
                self._seen.popitem(last=False)
            return seq


def workflow_event_id(
    workflow_id: str, run_id: str, history_length: int, seq: int, event_name: str
) -> str:
    key = f"{workflow_id}:{run_id}:{history_length}:{seq}:{event_name}"
    return uuid.uuid5(WORKFLOW_EVENT_NAMESPACE, key).hex


def _current_history_length() -> int | None:
    """Workflow スレッドの中でだけ値がある。外（Activity・API）では ``None``。"""
    try:
        if not _workflow.in_workflow():
            return None
        return _workflow.info().get_current_history_length()
    except Exception:
        return None


class ServiceIdentity:
    def __init__(self, service_name: str, environment: str, git_sha: str) -> None:
        self.service_name = service_name or UNKNOWN
        self.environment = environment if environment in _ENVIRONMENTS else UNKNOWN
        self.git_sha = git_sha or UNKNOWN

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> ServiceIdentity:
        from contracts.log_contract import ENV_ENVIRONMENT, ENV_GIT_REVISION, ENV_SERVICE_NAME

        return cls(
            env.get(ENV_SERVICE_NAME, ""),
            env.get(ENV_ENVIRONMENT, ""),
            env.get(ENV_GIT_REVISION, ""),
        )


class _Event:
    """1イベントの組み立て。安全化・型の写し・切り詰めの事実を貯める。"""

    def __init__(self) -> None:
        self.redactor = Redactor()
        self.fields: dict[str, Any] = {}
        self.attributes: dict[str, Any] = {}
        self.truncated = False
        self.response_truncated = False
        self.stack_truncated = False

    def keyword(self, value: Any) -> str:
        text = self.redactor.text(value if isinstance(value, str) else str(value))
        if len(text) > KEYWORD_MAX_CHARS:
            self.truncated = True
            text = text[:KEYWORD_MAX_CHARS]
        return text

    def put(self, name: str, value: Any) -> None:
        """契約の型に写す。写せない値・未知のキーは ``attributes`` へ（黙って捨てない）。"""
        if value is None:
            return
        if name == "attributes":
            if isinstance(value, Mapping):
                for k, v in value.items():
                    self.attributes[str(k)] = v
            else:
                self.attributes["attributes"] = value
            return
        ftype = _FIELD_TYPES.get(name)
        if ftype is None or name in _COLLECTOR_FIELDS or ftype is FieldType.DATE:
            self.attributes[name] = value
            return
        try:
            self.fields[name] = self._coerce(name, ftype, value)
        except (TypeError, ValueError):
            self.attributes[name] = value

    def _coerce(self, name: str, ftype: FieldType, value: Any) -> Any:
        if ftype is FieldType.KEYWORD:
            if isinstance(value, list | tuple | set | frozenset):
                return [self.keyword(v) for v in list(value)[:50]]
            if hasattr(value, "value") and isinstance(value.value, str):  # StrEnum 以外の Enum
                return self.keyword(value.value)
            return self.keyword(value)
        if ftype in (FieldType.INTEGER, FieldType.LONG):
            if isinstance(value, bool) or not isinstance(value, int | str):
                raise TypeError(name)
            return int(value)
        if ftype is FieldType.DOUBLE:
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise TypeError(name)
            return float(value)
        if ftype is FieldType.BOOLEAN:
            if not isinstance(value, bool):
                raise TypeError(name)
            return value
        if ftype is FieldType.TEXT:
            if name == "response_excerpt" and not isinstance(value, str):
                value = json.dumps(self.redactor.value(value), ensure_ascii=False, default=str)
            text = self.redactor.text(str(value))
            limit = {
                "error_message": ERROR_MESSAGE_MAX_BYTES,
                "response_excerpt": RESPONSE_EXCERPT_MAX_BYTES,
                "exception_stack": EXCEPTION_STACK_MAX_BYTES,
                "message": MESSAGE_MAX_BYTES,
            }.get(name, MESSAGE_MAX_BYTES)
            text, cut = truncate_bytes(text, limit)
            if cut:
                if name == "response_excerpt":
                    self.response_truncated = True
                elif name == "exception_stack":
                    self.stack_truncated = True
                else:
                    self.truncated = True
            return text
        raise TypeError(name)


class JsonFormatter(logging.Formatter):
    """``LogRecord`` → 1行 JSON（改行なし、``ensure_ascii=False``、≤ ``EVENT_MAX_BYTES``）。"""

    def __init__(self, identity: ServiceIdentity) -> None:
        super().__init__()
        self._identity = identity
        self._seq = _WorkflowSeq()

    # ------------------------------------------------------------------ 公開

    def format(self, record: logging.LogRecord) -> str:
        try:
            return self._serialize(self.build(record))
        except Exception as exc:  # 整形の失敗は業務に伝播させない（log-contract §7.9）
            return self._fallback(record, exc)

    def build(self, record: logging.LogRecord) -> dict[str, Any]:
        event = _Event()
        avp = getattr(record, RECORD_EXTRA_KEY, None)
        avp = dict(avp) if isinstance(avp, Mapping) else {}
        event_name = str(avp.pop("event_name", "") or EventName.LOG_RECORD.value)

        # 文脈 → Temporal の info → 発行側の値（後ろが勝つ）
        for key, value in current_context().items():
            event.put(key, value)
        workflow_info = getattr(record, "temporal_workflow", None)
        if isinstance(workflow_info, Mapping):
            for src, dst in (
                ("workflow_id", "workflow_id"),
                ("run_id", "run_id"),
                ("workflow_type", "workflow_type"),
                ("task_queue", "task_queue"),
            ):
                event.put(dst, workflow_info.get(src))
        activity_info = getattr(record, "temporal_activity", None)
        if isinstance(activity_info, Mapping):
            for src, dst in (
                ("activity_id", "activity_id"),
                ("activity_type", "activity_type"),
                ("attempt", "activity_attempt"),
                ("task_queue", "task_queue"),
                ("workflow_id", "workflow_id"),
                ("workflow_run_id", "run_id"),
                ("workflow_type", "workflow_type"),
            ):
                event.put(dst, activity_info.get(src))
        for key, value in avp.items():
            if key in _OWN_FIELDS:
                event.attributes[f"shadowed.{key}"] = value
                continue
            event.put(key, value)

        exc = record.exc_info[1] if record.exc_info else None
        if isinstance(exc, BaseException):
            if "error_type" not in event.fields:
                event.put("error_type", exception_type_name(exc))
            if "error_message" not in event.fields:
                event.put("error_message", str(exc))
            stack, cut = format_exception_chain(exc)
            event.put("exception_stack", stack)
            event.stack_truncated = event.stack_truncated or cut
        elif record.stack_info:
            event.put("exception_stack", record.stack_info)

        message, cut = truncate_bytes(event.redactor.text(record.getMessage()), MESSAGE_MAX_BYTES)
        event.truncated = event.truncated or cut

        out: dict[str, Any] = {
            "@timestamp": _timestamp(record.created),
            "schema_version": LOG_SCHEMA_VERSION,
            "event_id": self._event_id(event_name, workflow_info),
            "event_name": event.keyword(event_name),
            "level": _level_name(record.levelno),
            "message": message,
            "service_name": self._identity.service_name,
            "environment": self._identity.environment,
            "git_sha": self._identity.git_sha,
            "logger": event.keyword(record.name),
        }
        for key, value in event.fields.items():
            if key not in out:
                out[key] = value
        if event.attributes:
            attrs = event.redactor.value(event.attributes)
            attrs, cut = _fit_attributes(attrs)
            event.truncated = event.truncated or cut
            out["attributes"] = attrs
        if event.redactor.applied:
            out["redaction_applied"] = True
        if event.response_truncated:
            out["response_truncated"] = True
        if event.stack_truncated:
            out["stack_truncated"] = True
        if event.truncated:
            out["truncated"] = True
        return out

    # ------------------------------------------------------------------ 内部

    def _event_id(self, event_name: str, workflow_info: Any) -> str:
        if isinstance(workflow_info, Mapping):
            history_length = _current_history_length()
            run_id = workflow_info.get("run_id")
            workflow_id = workflow_info.get("workflow_id")
            if history_length is not None and run_id and workflow_id:
                seq = self._seq.next(str(run_id), history_length)
                return workflow_event_id(
                    str(workflow_id), str(run_id), history_length, seq, event_name
                )
        return uuid.uuid4().hex

    @staticmethod
    def _serialize(event: dict[str, Any]) -> str:
        line = json.dumps(event, ensure_ascii=False, default=str, separators=(",", ":"))
        if len(line.encode("utf-8")) <= EVENT_MAX_BYTES:
            return line
        # attributes → exception_stack → response_excerpt → message の順に削る（log-contract §1）
        event["truncated"] = True
        event.pop("attributes", None)
        for name, flag in (
            ("exception_stack", "stack_truncated"),
            ("response_excerpt", "response_truncated"),
            ("message", None),
        ):
            line = json.dumps(event, ensure_ascii=False, default=str, separators=(",", ":"))
            over = len(line.encode("utf-8")) - EVENT_MAX_BYTES
            if over <= 0:
                return line
            value = event.get(name)
            if isinstance(value, str):
                keep = max(0, len(value.encode("utf-8")) - over - 64)
                event[name], _ = truncate_bytes(value, keep)
                if flag:
                    event[flag] = True
        line = json.dumps(event, ensure_ascii=False, default=str, separators=(",", ":"))
        if len(line.encode("utf-8")) <= EVENT_MAX_BYTES:
            return line
        # それでも溢れる（keyword が多い等）: 任意フィールドを落として必須だけにする
        from contracts.log_contract import REQUIRED_APP_FIELDS

        minimal = {k: event[k] for k in REQUIRED_APP_FIELDS if k in event}
        minimal["truncated"] = True
        return json.dumps(minimal, ensure_ascii=False, default=str, separators=(",", ":"))

    def _fallback(self, record: logging.LogRecord, exc: BaseException) -> str:
        try:
            level = _level_name(int(getattr(record, "levelno", logging.ERROR)))
            name = str(getattr(record, "name", UNKNOWN))[:KEYWORD_MAX_CHARS]
            created = float(getattr(record, "created", 0.0)) or datetime.now(UTC).timestamp()
        except Exception:
            level, name, created = "ERROR", UNKNOWN, datetime.now(UTC).timestamp()
        minimal = {
            "@timestamp": _timestamp(created),
            "schema_version": LOG_SCHEMA_VERSION,
            "event_id": uuid.uuid4().hex,
            "event_name": EventName.LOG_RECORD.value,
            "level": level,
            "message": "log formatting failed",
            "service_name": self._identity.service_name,
            "environment": self._identity.environment,
            "git_sha": self._identity.git_sha,
            "logger": name,
            "error_type": type(exc).__name__,
        }
        return json.dumps(minimal, ensure_ascii=False, separators=(",", ":"))


def _fit_attributes(attrs: Any) -> tuple[Any, bool]:
    if _json_size(attrs) <= ATTRIBUTES_MAX_BYTES:
        return attrs, False
    if not isinstance(attrs, dict):
        return {"dropped": True}, True
    kept = dict(attrs)
    dropped: list[str] = []
    for key in sorted(kept, key=lambda k: _json_size(kept[k]), reverse=True):
        if _json_size(kept) <= ATTRIBUTES_MAX_BYTES - 256:
            break
        kept.pop(key)
        dropped.append(key)
    kept["dropped_keys"] = dropped[:20]
    if _json_size(kept) > ATTRIBUTES_MAX_BYTES:
        return {"dropped": True}, True
    return kept, True


class SafeTextFormatter(logging.Formatter):
    """``AVP_LOG_FORMAT=text``（rollback 用）。従来の basicConfig 形式に安全化だけをかける。"""

    def __init__(self) -> None:
        super().__init__(logging.BASIC_FORMAT)

    def format(self, record: logging.LogRecord) -> str:
        try:
            text, _ = sanitize_text(super().format(record))
            return text
        except Exception as exc:
            name = getattr(record, "name", UNKNOWN)
            return f"ERROR:{name}:log formatting failed ({type(exc).__name__})"


__all__ = [
    "WORKFLOW_EVENT_NAMESPACE",
    "JsonFormatter",
    "SafeTextFormatter",
    "ServiceIdentity",
    "exception_type_name",
    "format_exception_chain",
    "truncate_bytes",
    "workflow_event_id",
]
