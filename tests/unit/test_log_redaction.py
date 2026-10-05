"""秘密が stdout に出ないこと（INV-39 / log-contract §7）。

キー名・値のパターン・URL・DSN・fal key・JWT・private key・例外文・第三者 logger・未捕捉例外・
Temporal Core の転送を、整形器の出力（実際に stdout に書く行）で確かめる。
理由は docs/testing/logging-rationale.md。
"""

from __future__ import annotations

import io
import json
import logging
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

from contracts.log_contract import REDACTED
from infrastructure.logging.formatter import JsonFormatter, ServiceIdentity
from infrastructure.logging.redaction import (
    allowed_hosts,
    is_secret_key,
    sanitize_text,
    sanitize_url,
)
from infrastructure.youtube.uploader import UPLOAD_URL

REPO = Path(__file__).resolve().parents[2]
FAL_KEY = "0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b:" + "ab" * 16
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
PRIVATE_KEY = (
    "-----BEGIN RSA PRIVATE KEY-----\nMIIEow" + "A" * 40 + "\n-----END RSA PRIVATE KEY-----"
)
SECRETS = [
    FAL_KEY,
    JWT,
    "MIIEow",
    "hunter2pass",
    "ya29.a0AfH6SMBx-secret-token",
    "1//0gLongRefreshTokenValue",
    "sk-proj-abcdefgh12345",
    "sessionid=supersecret",
    "upload_id=AEnB2Uo-secret",
]


def _line(record: logging.LogRecord) -> str:
    return JsonFormatter(ServiceIdentity("svc", "test", "sha")).format(record)


def _record(msg: str, *, avp: dict[str, Any] | None = None, exc: BaseException | None = None):
    exc_info = (type(exc), exc, exc.__traceback__) if exc is not None else None
    record = logging.LogRecord("httpx", logging.INFO, __file__, 1, msg, (), exc_info)
    if avp is not None:
        record.avp = avp
    return record


def _assert_clean(line: str) -> None:
    for secret in SECRETS:
        assert secret not in line, secret


@pytest.mark.parametrize(
    "key",
    [
        "Authorization",
        "set-cookie",
        "refresh_token",
        "client_secret",
        "FAL_KEY",
        "x-amz-security-token",
        "database_url",
        "session_uri",
        "key",
        "code",
        "location",
        "upload_id",
        "minio_access_key",
    ],
)
def test_secret_key_names(key: str) -> None:
    assert is_secret_key(key)


@pytest.mark.parametrize("key", ["episode_id", "scene_id", "input_hash", "keyword", "status"])
def test_ordinary_key_names(key: str) -> None:
    assert not is_secret_key(key)


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("Authorization: Bearer abcdefghijklmnop", "abcdefghijklmnop"),
        ("header Key " + FAL_KEY, FAL_KEY),
        ("fal key " + FAL_KEY + " rejected", FAL_KEY),
        ("token " + JWT, JWT),
        (PRIVATE_KEY, "MIIEow"),
        ("dsn postgresql+psycopg://avp:hunter2pass@postgres:5432/avp", "hunter2pass"),
        ("oauth ya29.a0AfH6SMBx-secret-token here", "ya29.a0AfH6SMBx-secret-token"),
        ("refresh 1//0gLongRefreshTokenValue", "1//0gLongRefreshTokenValue"),
        ("openai sk-proj-abcdefgh12345", "sk-proj-abcdefgh12345"),
        ("FAL_KEY=abc123secret failed", "abc123secret"),
        ('{"access_token": "tok-123456"}', "tok-123456"),
        (
            "(psycopg.errors.X) boom\n[SQL: INSERT INTO t VALUES (%s)]\n"
            "[parameters: ('the whole prompt text', 'secret-ish')]\n"
            "(Background on this error at: x)",
            "the whole prompt text",
        ),
        ("image " + "QUJD" * 100, "QUJD" * 100),
    ],
)
def test_value_patterns_are_replaced(text: str, secret: str) -> None:
    out, changed = sanitize_text(text)
    assert changed
    assert secret not in out
    assert REDACTED in out


def test_allowed_hosts_are_derived_from_the_adapter_constants() -> None:
    from infrastructure.analytics.youtube_analytics import REPORTS_ENDPOINT
    from infrastructure.providers.fal_queue import QUEUE_BASE_URL
    from infrastructure.providers.fal_storage import STORAGE_TOKEN_URL
    from infrastructure.youtube.oauth import TOKEN_ENDPOINT
    from infrastructure.youtube.uploader import API_BASE_URL

    expected = {
        urlsplit(u).hostname
        for u in (
            QUEUE_BASE_URL,
            STORAGE_TOKEN_URL,
            UPLOAD_URL,
            API_BASE_URL,
            TOKEN_ENDPOINT,
            REPORTS_ENDPOINT,
        )
    }
    assert expected <= allowed_hosts()
    assert "v3.fal.media" not in allowed_hosts()


def test_allowed_host_keeps_path_but_drops_query_and_userinfo() -> None:
    # endpoint は adapter の定数から組む（literal を書かない / INV-18 の検査と同じ方針）
    from infrastructure.providers.fal_queue import QUEUE_BASE_URL

    path = f"{QUEUE_BASE_URL}/model-x/requests/abc/status"
    assert sanitize_url(f"{path}?token=zzz#frag") == path
    resumable = f"{UPLOAD_URL}?uploadType=resumable&upload_id=AEnB2Uo-secret"
    assert "AEnB2Uo" not in sanitize_url(resumable)


def test_other_hosts_are_shrunk_to_a_hash() -> None:
    url = "https://v3.fal.media/files/lion/secret-capability-path.png?x=1"
    out = sanitize_url(url)
    assert out.startswith("https://v3.fal.media/…#sha256:")
    assert "secret-capability-path" not in out
    # 二度かけても変わらない（adapter の伏せ字処理の出力に重ねても壊れない）
    assert sanitize_url(out) == out
    again, changed = sanitize_text(f"see {out}.")
    assert again == f"see {out}." and not changed


def test_sanitize_is_idempotent_on_already_redacted_text() -> None:
    once, _ = sanitize_text("Bearer abcdefghijklmnop api_key=abc123secret")
    twice, changed = sanitize_text(once)
    assert twice == once and not changed


def test_message_attributes_and_exception_text_are_cleaned() -> None:
    with pytest.raises(RuntimeError) as caught:
        try:
            raise ValueError(f"fal refused key {FAL_KEY} token {JWT}")
        except ValueError as inner:
            raise RuntimeError("wrapped: postgresql+psycopg://avp:hunter2pass@db/avp") from inner
    err = caught.value
    line = _line(
        _record(
            f"HTTP Request: PUT {UPLOAD_URL}"
            '?uploadType=resumable&upload_id=AEnB2Uo-secret "HTTP/1.1 308"',
            avp={
                "attributes": {
                    "headers": {
                        "Authorization": "Bearer hunter2pass",
                        "cookie": "sessionid=supersecret",
                    }
                },
                "private": PRIVATE_KEY,
                "response_excerpt": {
                    "refresh_token": "1//0gLongRefreshTokenValue",
                    "msg": "ya29.a0AfH6SMBx-secret-token",
                },
            },
            exc=err,
        )
    )
    _assert_clean(line)
    event = json.loads(line)
    assert event["redaction_applied"] is True
    assert event["attributes"]["headers"]["Authorization"] == REDACTED


def _configure(stream: io.StringIO) -> None:
    from infrastructure.logging.setup import configure_logging

    configure_logging(
        {"AVP_SERVICE_NAME": "svc", "AVP_ENVIRONMENT": "test"},
        stream,
        forward_temporal_core=False,
    )


@pytest.fixture
def restore_logging():
    root = logging.getLogger()
    saved = (list(root.handlers), root.level, sys.excepthook, logging.raiseExceptions)
    levels = {n: logging.getLogger(n).level for n in ("httpx", "httpcore", "temporalio")}
    yield
    root.handlers[:] = saved[0]
    root.setLevel(saved[1])
    sys.excepthook = saved[2]
    logging.raiseExceptions = saved[3]
    logging.captureWarnings(False)
    import threading

    threading.excepthook = threading.__excepthook__
    for name, level in levels.items():
        logging.getLogger(name).setLevel(level)


def test_third_party_logger_goes_through_the_same_formatter(restore_logging) -> None:
    stream = io.StringIO()
    _configure(stream)
    logging.getLogger("sqlalchemy.engine").warning(
        "boom [parameters: ('secret-ish prompt',)]\n(Background on this error at: x)"
    )
    logging.getLogger("httpx").info(
        "HTTP Request: GET https://x/?token=hunter2pass"
    )  # WARNING 未満
    out = stream.getvalue()
    assert "secret-ish prompt" not in out
    assert "hunter2pass" not in out
    assert json.loads(out.splitlines()[0])["logger"] == "sqlalchemy.engine"


def test_uncaught_exception_goes_through_the_formatter(restore_logging) -> None:
    stream = io.StringIO()
    _configure(stream)
    try:
        raise RuntimeError(f"crash with {FAL_KEY}")
    except RuntimeError as exc:
        sys.excepthook(type(exc), exc, exc.__traceback__)
    event = json.loads(stream.getvalue().splitlines()[-1])
    assert event["level"] == "CRITICAL" and event["logger"] == "avp.uncaught"
    assert FAL_KEY not in stream.getvalue()


def test_warnings_are_captured(restore_logging) -> None:
    import warnings

    stream = io.StringIO()
    _configure(stream)
    warnings.warn("deprecated with api_key=abc123secret", UserWarning, stacklevel=1)
    assert "abc123secret" not in stream.getvalue()
    assert json.loads(stream.getvalue().splitlines()[-1])["logger"] == "py.warnings"


def test_configure_logging_does_not_stack_handlers(restore_logging) -> None:
    stream = io.StringIO()
    _configure(stream)
    _configure(stream)
    assert len(logging.getLogger().handlers) == 1
    assert logging.getLogger("httpx").level == logging.WARNING


def test_text_format_is_available_for_rollback_and_still_sanitizes(restore_logging) -> None:
    from infrastructure.logging.setup import configure_logging

    stream = io.StringIO()
    configure_logging({"AVP_LOG_FORMAT": "text"}, stream, forward_temporal_core=False)
    logging.getLogger("worker_entry").info("starting FAL_KEY=abc123secret")
    assert stream.getvalue() == f"INFO:worker_entry:starting FAL_KEY={REDACTED}\n"


def test_temporal_core_logs_are_forwarded_not_written_to_stderr() -> None:
    """Core（Rust）の既定は console へ非 JSON で直書き（実測では stdout に ANSI 付きの行）。
    転送を置いた Runtime では整形器を通った JSON になる（ADR-0040 §1）。

    Runtime はプロセスで1つなので子プロセスで確かめる。time-skipping の test server は
    Worker 起動時に Core の WARN を出す（Activity を持つ Worker の起動時。実測）。
    """
    script = textwrap.dedent(
        """
        import asyncio, logging, sys
        from infrastructure.logging.setup import configure_logging
        configure_logging({"AVP_SERVICE_NAME": "probe"}, sys.stdout)
        from temporalio import activity, workflow
        from temporalio.testing import WorkflowEnvironment
        from temporalio.worker import Worker

        @activity.defn
        async def noop() -> None:
            return None

        @workflow.defn(sandboxed=False)
        class Probe:
            @workflow.run
            async def run(self) -> str:
                return "ok"

        async def main():
            async with await WorkflowEnvironment.start_time_skipping() as env:
                async with Worker(
                    env.client, task_queue="probe", workflows=[Probe], activities=[noop]
                ):
                    await env.client.execute_workflow(Probe.run, id="probe", task_queue="probe")

        asyncio.run(main())
        """
    )
    done = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    assert "temporalio_sdk_core" not in done.stderr
    lines = [line for line in done.stdout.splitlines() if line.strip()]
    # stdout の全行が JSON（Core の console 出力が混ざらない）
    events = [json.loads(line) for line in lines]
    assert events, done.stdout[-2000:]
    core = [e for e in events if e["logger"].startswith("temporalio.core")]
    assert core, [e["logger"] for e in events]
    assert all(e["service_name"] == "probe" for e in events)
