"""release preflight の collector（読み取り専用。ADR-0050 / INV-65）。

呼ぶものは git の読み取り・``docker ps/inspect``・``gh api``（GET）・DB の SELECT
（read-only 接続）・Temporal の describe/list・ファイルの読み取りだけ。書き込み・再起動・
Schedule の変更・有料 API・YouTube は呼ばない
（tests/architecture/test_release_preflight_read_only.py が許可リストで固定する）。
失敗は ``CollectorUnavailable`` にして、判定側が UNKNOWN に変える。秘密は出力しない。
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import subprocess
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.engine import make_url

from domain.provider.reconciliation import request_id_from_ref
from domain.release_preflight import (
    APP_IMAGES,
    COMPOSE_PROJECT,
    REVISION_LABEL,
    CheckRun,
    CollectorUnavailable,
    HeadState,
    Relation,
    ReservationView,
    TargetInfo,
)
from infrastructure.config import Settings
from infrastructure.release.app_services import expected_running_services
from infrastructure.release.compose_queues import required_queues
from infrastructure.temporal.poller_check import queues_missing_pollers, temporal_describe_factory

MIGRATIONS_DIR = "infrastructure/db/migrations/versions/"
ADR_DIR = "docs/decisions/"
COMMAND_TIMEOUT_SECONDS = 30
DB_CONNECT_TIMEOUT_SECONDS = 5


def _run(cmd: Sequence[str], cwd: pathlib.Path) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(  # noqa: S603 - 引数は固定の読み取りコマンド
            list(cmd), cwd=cwd, capture_output=True, text=True, timeout=COMMAND_TIMEOUT_SECONDS
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CollectorUnavailable(f"{cmd[0]}: {type(exc).__name__}") from exc


def _ok(cmd: Sequence[str], cwd: pathlib.Path) -> str:
    done = _run(cmd, cwd)
    if done.returncode != 0:
        raise CollectorUnavailable(f"{cmd[0]} {cmd[1]} exited {done.returncode}")
    return done.stdout


def _dotenv_value(path: pathlib.Path, key: str) -> str | None:
    """``.env`` から1つのキーを読む（Settings は未知のキーを捨てるので自前で読む）。"""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        name, sep, value = line.strip().partition("=")
        if sep and name.strip() == key:
            return value.strip().strip("\"'")
    return None


class RealCollectors:
    def __init__(self, repo: pathlib.Path, settings: Settings | None = None) -> None:
        self._repo = repo
        self._settings = settings

    @property
    def settings(self) -> Settings:
        if self._settings is None:
            # cwd の .env ではなく repo root の .env（どこから実行しても同じ対象を読む）
            self._settings = Settings(_env_file=self._repo / ".env")  # pyright: ignore[reportCallIssue]
        return self._settings

    def target_info(self) -> TargetInfo:
        url = make_url(self.settings.database_url)
        environment = os.environ.get("AVP_ENVIRONMENT") or _dotenv_value(
            self._repo / ".env", "AVP_ENVIRONMENT"
        )
        return TargetInfo(
            db_host=url.host or "",
            db_name=url.database or "",
            temporal_address=self.settings.temporal_address,
            temporal_namespace=self.settings.temporal_namespace,
            environment=environment or "",
        )

    # ---- git
    def commit_exists(self, sha: str) -> bool:
        return _run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], self._repo).returncode == 0

    def head_state(self) -> HeadState:
        head = _ok(["git", "rev-parse", "HEAD"], self._repo).strip()
        # release/ の記録（承認・証跡）は deploy 対象のコードではないので、dirty に数えない
        status = _ok(["git", "status", "--porcelain", "--", ".", ":(exclude)release"], self._repo)
        return HeadState(head_sha=head, dirty=bool(status.strip()))

    def ancestry(self, deployed: str, target: str) -> Relation:
        if not self.commit_exists(deployed):
            return Relation.UNKNOWN_COMMIT
        if deployed == target:
            return Relation.SAME
        if (
            _run(["git", "merge-base", "--is-ancestor", deployed, target], self._repo).returncode
            == 0
        ):
            return Relation.FAST_FORWARD
        if (
            _run(["git", "merge-base", "--is-ancestor", target, deployed], self._repo).returncode
            == 0
        ):
            return Relation.BEHIND
        return Relation.DIVERGED

    def migration_files(self, rev: str) -> Mapping[str, str]:
        names = _ok(["git", "ls-tree", "--name-only", rev, MIGRATIONS_DIR], self._repo).split()
        return {
            pathlib.PurePosixPath(n).name: _ok(["git", "show", f"{rev}:{n}"], self._repo)
            for n in names
            if n.endswith(".py")
        }

    def adr_text(self, rev: str, number: str) -> str | None:
        """``rev`` の ADR ``<number>-*.md`` の本文。無ければ None。"""
        listing = _ok(["git", "ls-tree", "--name-only", rev, ADR_DIR], self._repo)
        names = listing.split()
        for n in names:
            if pathlib.PurePosixPath(n).name.startswith(f"{number}-") and n.endswith(".md"):
                return _ok(["git", "show", f"{rev}:{n}"], self._repo)
        return None

    # ---- docker（inspect / ps だけ）
    def container_revisions(self) -> Mapping[str, str | None]:
        ids = _ok(
            [
                "docker",
                "ps",
                "--filter",
                f"label=com.docker.compose.project={COMPOSE_PROJECT}",
                "-q",
            ],
            self._repo,
        ).split()
        if not ids:
            return {}
        fmt = (
            '{{index .Config.Labels "com.docker.compose.service"}}\t{{.Config.Image}}\t'
            f'{{{{index .Config.Labels "{REVISION_LABEL}"}}}}'
        )
        out = _ok(["docker", "inspect", "--format", fmt, *ids], self._repo)
        revisions: dict[str, str | None] = {}
        for line in out.splitlines():
            service, image, revision = (line.split("\t") + ["", ""])[:3]
            if image in APP_IMAGES:
                revisions[service] = revision or None
        return revisions

    def expected_services(self) -> Sequence[str]:
        script = (self._repo / "scripts" / "deploy-workers.sh").read_text(encoding="utf-8")
        compose = (self._repo / "compose.yaml").read_text(encoding="utf-8")
        return expected_running_services(script, compose)

    # ---- CI（gh api の GET だけ）
    def ci_check_runs(self, sha: str) -> Sequence[CheckRun]:
        out = _ok(
            [
                "gh",
                "api",
                f"repos/{{owner}}/{{repo}}/commits/{sha}/check-runs",
                "--paginate",
                "--jq",
                '.check_runs[] | [.name, .status, (.conclusion // "")] | @tsv',
            ],
            self._repo,
        )
        runs = []
        for line in out.splitlines():
            name, status, conclusion = (line.split("\t") + ["", ""])[:3]
            runs.append(CheckRun(name, status, conclusion or None))
        return runs

    # ---- DB（SELECT だけ。接続自体を read-only にする）
    def _select_one(self, sql: str) -> Any:
        import psycopg

        url = make_url(self.settings.database_url).set(drivername="postgresql")
        try:
            with psycopg.connect(
                url.render_as_string(hide_password=False),
                connect_timeout=DB_CONNECT_TIMEOUT_SECONDS,
                options="-c default_transaction_read_only=on",
            ) as conn:
                row = conn.execute(sql).fetchone()  # type: ignore[arg-type]
        except psycopg.Error as exc:
            raise CollectorUnavailable(f"database: {type(exc).__name__}") from exc
        return None if row is None else row[0]

    def _select_all(self, sql: str) -> list[Any]:
        import psycopg

        url = make_url(self.settings.database_url).set(drivername="postgresql")
        try:
            with psycopg.connect(
                url.render_as_string(hide_password=False),
                connect_timeout=DB_CONNECT_TIMEOUT_SECONDS,
                options="-c default_transaction_read_only=on",
            ) as conn:
                return list(conn.execute(sql).fetchall())  # type: ignore[arg-type]
        except psycopg.Error as exc:
            raise CollectorUnavailable(f"database: {type(exc).__name__}") from exc

    def db_alembic_version(self) -> str | None:
        value = self._select_one("SELECT version_num FROM alembic_version")
        return None if value is None else str(value)

    def pending_reservation_count(self) -> int:
        return int(
            self._select_one("SELECT count(*) FROM provider_reservations WHERE status = 'reserved'")
        )

    def pending_reservations(self) -> list[ReservationView]:
        """未照合（``reserved``）の予約行。waiver の照合用（ADR-0062）。SELECT だけ。"""
        rows = self._select_all(
            "SELECT id, provider, status, provider_job_ref, estimated_cost_usd, reserved_at,"
            " dispatched_at, raw_output_key, outcome_artifact_id"
            " FROM provider_reservations WHERE status = 'reserved' ORDER BY id"
        )

        def text(value: Any) -> str | None:
            return None if value is None else str(value)

        return [
            ReservationView(
                id=str(r[0]),
                provider=str(r[1]),
                status=str(r[2]),
                provider_request_id=request_id_from_ref(r[3]),
                estimated_cost_usd=text(r[4]),
                reserved_at=r[5].isoformat(),
                dispatched_at=None if r[6] is None else r[6].isoformat(),
                raw_output_key=text(r[7]),
                outcome_artifact_id=text(r[8]),
            )
            for r in rows
        ]

    def paid_budget_setting(self) -> str | None:
        """``FAL_MONTHLY_BUDGET_USD`` の生の値。環境変数が先、次に repo の ``.env``。"""
        return os.environ.get("FAL_MONTHLY_BUDGET_USD") or _dotenv_value(
            self._repo / ".env", "FAL_MONTHLY_BUDGET_USD"
        )

    def unhealthy_services(self) -> list[str]:
        """compose project の container で Health が unhealthy のもの（読み取りのみ）。"""
        ids = _ok(
            [
                "docker",
                "ps",
                "--filter",
                f"label=com.docker.compose.project={COMPOSE_PROJECT}",
                "--filter",
                "health=unhealthy",
                "--format",
                '{{.Label "com.docker.compose.service"}}',
            ],
            self._repo,
        )
        return sorted(set(ids.split()))

    # ---- Temporal（describe / list だけ）
    def _temporal(self) -> Any:
        from temporalio.client import Client

        async def connect() -> Client:
            return await asyncio.wait_for(
                Client.connect(
                    self.settings.temporal_address, namespace=self.settings.temporal_namespace
                ),
                timeout=10,
            )

        return connect

    def running_workflow_count(self) -> int:
        connect = self._temporal()

        async def count() -> int:
            client = await connect()
            n = 0
            async for _ in client.list_workflows('ExecutionStatus="Running"'):
                n += 1
            return n

        try:
            return asyncio.run(count())
        except Exception as exc:
            raise CollectorUnavailable(f"temporal: {type(exc).__name__}") from exc

    def schedule_states(self) -> Mapping[str, Mapping[str, object]]:
        from contracts.schedule_guard import WATCHDOG_SCHEDULE_ID
        from domain.schedule_guard import parse_maintenance_note
        from infrastructure.temporal.schedules import TemporalScheduleControl

        connect = self._temporal()
        ids = (self.settings.daily_schedule_id, WATCHDOG_SCHEDULE_ID)

        async def read() -> dict[str, dict[str, object]]:
            control = TemporalScheduleControl(await connect())
            out: dict[str, dict[str, object]] = {}
            for schedule_id in ids:
                snap = await control.describe(schedule_id)
                out[schedule_id] = {
                    "exists": snap.exists,
                    "paused": snap.paused,
                    "maintenance": parse_maintenance_note(snap.note) is not None,
                }
            return out

        try:
            return asyncio.run(read())
        except Exception as exc:
            raise CollectorUnavailable(f"temporal: {type(exc).__name__}") from exc

    def required_queues(self) -> Sequence[str]:
        return sorted(self._queue_ages())

    def _queue_ages(self) -> dict[str, float]:
        return required_queues((self._repo / "compose.yaml").read_text(encoding="utf-8"))

    def queues_without_poller(self, queues: Sequence[str]) -> Sequence[str]:
        ages = self._queue_ages()
        describe = temporal_describe_factory(
            self.settings.temporal_address, self.settings.temporal_namespace
        )
        from datetime import timedelta

        async def check() -> list[str]:
            missing: list[str] = []
            now = datetime.now(UTC)
            for queue in queues:
                pollers = await describe(queue)
                # host から見るので identity の接尾辞（コンテナの hostname）は問わない
                missing += queues_missing_pollers(
                    {queue: pollers},
                    suffix="",
                    now=now,
                    max_age=timedelta(seconds=ages.get(queue, 120.0)),
                )
            return missing

        try:
            return asyncio.run(check())
        except Exception as exc:
            raise CollectorUnavailable(f"temporal: {type(exc).__name__}") from exc

    # ---- ファイル
    def read_text(self, path: str) -> str | None:
        p = pathlib.Path(path)
        if not p.is_absolute():
            p = self._repo / p
        try:
            return p.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise CollectorUnavailable(f"{type(exc).__name__}") from exc
