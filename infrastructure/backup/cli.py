"""``scripts/backup.py`` の実体。サブコマンド: run / restore-drill / prune / verify / status。

終了コード: 0 成功 / 1 失敗（audit 行と通知済み）/ 2 使い方の誤り・拒否（復旧先が本番など）。
本番へ向けて実行するのは run（読み取りのみ）だけ。restore-drill は使い捨ての復旧先にだけ書く。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import pathlib
import secrets
import shlex
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from contracts.backup import (
    BACKUP_DATABASES,
    DEFAULT_KEEP_DAILY,
    DEFAULT_KEEP_MONTHLY,
    DEFAULT_KEEP_WEEKLY,
    ENV_DB_ROOT,
    ENV_OBJECT_ROOT,
    ENV_OFFSITE_CMD,
    EXIT_FAILED,
    EXIT_OK,
    EXIT_REFUSED,
    PRODUCTION_MINIO_CONTAINER,
    PRODUCTION_POSTGRES_CONTAINER,
    RESTORE_BUCKET_PREFIX,
    RESTORE_CONTAINER_PREFIX,
)
from contracts.pipeline import DEFAULT_DAILY_SCHEDULE_CRON, DEFAULT_SCHEDULE_TIMEZONE
from contracts.schedule_guard import DEFAULT_WATCHDOG_CRON
from domain.backup.guards import ProductionTargetRefused, RestoreTarget
from domain.backup.retention import Policy
from infrastructure.backup import catalog
from infrastructure.backup.audit import AuditLog, notifier_from_env
from infrastructure.backup.mounts import mount_source
from infrastructure.backup.objects import MinioObjectSource, ObjectSource
from infrastructure.backup.postgres import PostgresAccess
from infrastructure.backup.precheck import (
    REQUIRED_CREDENTIALS,
    OsFs,
    PrecheckConfig,
    PrecheckDeps,
    Prechecker,
    render_human,
)
from infrastructure.backup.prune import prune
from infrastructure.backup.restore import DrillAborted, DrillDeps, RestoreDrill, restore_db_name
from infrastructure.backup.runner import CommandRunner, SubprocessRunner
from infrastructure.backup.service import BackupConfig, BackupDeps, BackupFailed, BackupService
from infrastructure.backup.verify import verify_files
from infrastructure.config import Settings
from infrastructure.workdir import MINIO_DATA_DIR

REPO = pathlib.Path(__file__).resolve().parents[2]
DEFAULT_WORK_ROOT = "/var/tmp"


class UsageError(Exception):
    pass


def _roots(args: argparse.Namespace, env: Mapping[str, str]) -> tuple[Path, Path]:
    db = args.db_root or env.get(ENV_DB_ROOT)
    obj = args.object_root or env.get(ENV_OBJECT_ROOT)
    if not db or not obj:
        raise UsageError(
            f"backup roots are not configured: set {ENV_DB_ROOT} and {ENV_OBJECT_ROOT} "
            "(or --db-root/--object-root). There is no default destination."
        )
    return Path(db).resolve(), Path(obj).resolve()


def _mount_device(runner: CommandRunner, container: str, destination: str) -> int:
    """コンテナの mount 元（host path）が載っているデバイス。確認できなければ失敗（安全側）。"""
    source = mount_source(runner, container, destination)
    if source is None:
        raise BackupFailed(
            f"cannot inspect {container} mount {destination}: "
            "cannot prove the destination is separate"
        )
    return os.stat(source).st_dev


def _git(args: Sequence[str]) -> str:
    done = subprocess.run(  # noqa: S603
        ["git", *args], cwd=REPO, capture_output=True, text=True, check=False, timeout=30
    )
    return done.stdout.strip() if done.returncode == 0 else ""


def _free_suffix() -> str:
    return secrets.token_hex(3)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="backup.py",
        description="復旧できるバックアップ（ADR-0051）。共通の引数はサブコマンドの前に置く。",
    )
    p.add_argument("--db-root", help=f"DB の写しの置き場（既定: ${ENV_DB_ROOT}）")
    p.add_argument("--object-root", help=f"オブジェクトの写しの置き場（既定: ${ENV_OBJECT_ROOT}）")
    p.add_argument("--env-file", help="MinIO の接続設定を読む .env（既定: repo root の .env）")
    sub = p.add_subparsers(dest="cmd", required=True)
    pre = sub.add_parser(
        "precheck", help="本番 backup 承認前の read-only 点検（何も作らず書かない）"
    )
    pre.add_argument("--bucket", action="append", help="対象 bucket（既定: Settings の bucket）")
    pre.add_argument("--postgres-container", default=PRODUCTION_POSTGRES_CONTAINER)
    pre.add_argument("--minio-container", default=PRODUCTION_MINIO_CONTAINER)
    pre.add_argument("--pg-user", default=os.environ.get("POSTGRES_USER", "avp"))
    pre.add_argument("--json-only", action="store_true", help="人向けの要約を出さない")
    run = sub.add_parser("run", help="backup を取る（本番へは読み取りだけ）")
    run.add_argument("--bucket", action="append", help="対象 bucket（既定: Settings の bucket）")
    run.add_argument(
        "--allow-same-device", action="store_true", help="保存先が同じディスクでも許す"
    )
    run.add_argument("--quiesce-note", default="none", help="静止したと運用者が申告する場合の注記")
    run.add_argument("--postgres-container", default=PRODUCTION_POSTGRES_CONTAINER)
    run.add_argument("--minio-container", default=PRODUCTION_MINIO_CONTAINER)
    run.add_argument("--pg-user", default=os.environ.get("POSTGRES_USER", "avp"))
    drill = sub.add_parser("restore-drill", help="隔離先へ復旧して検証し、証跡を manifest に書く")
    drill.add_argument("--backup-id", required=True, help="backup id か latest")
    drill.add_argument("--work-root", default=DEFAULT_WORK_ROOT)
    pr = sub.add_parser("prune", help="保持方針に従って検証済みの古い集合を消す（既定は dry-run）")
    pr.add_argument("--apply", action="store_true", help="実際に消す")
    pr.add_argument("--keep-daily", type=int, default=DEFAULT_KEEP_DAILY)
    pr.add_argument("--keep-weekly", type=int, default=DEFAULT_KEEP_WEEKLY)
    pr.add_argument("--keep-monthly", type=int, default=DEFAULT_KEEP_MONTHLY)
    ver = sub.add_parser("verify", help="全ファイルを再ハッシュする（restore はしない）")
    ver.add_argument("--backup-id", required=True)
    st = sub.add_parser("status", help="集合の一覧と鮮度")
    st.add_argument("--max-age-hours", type=float, default=26.0)
    return p


def main(
    argv: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
    *,
    runner: CommandRunner | None = None,
    objects: ObjectSource | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    env = os.environ if env is None else env
    args = build_parser().parse_args(argv)
    runner = runner or SubprocessRunner()
    if args.cmd == "precheck":  # root が無くても作らない・audit も残さない
        return _precheck(args, env, runner, now, objects)
    try:
        db_root, object_root = _roots(args, env)
        for r in dict.fromkeys((db_root, object_root)):
            if not r.is_dir():
                raise UsageError(f"backup root does not exist: {r}")
        audit = AuditLog(db_root / "audit.log")
        notifier = notifier_from_env(env)
        handler = {
            "run": _run,
            "restore-drill": _drill,
            "prune": _prune,
            "verify": _verify,
            "status": _status,
        }[args.cmd]
        return handler(args, env, db_root, object_root, runner, audit, notifier, now)
    except UsageError as exc:
        print(f"usage: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except ProductionTargetRefused as exc:
        print(f"refused: restore target is production: {exc}", file=sys.stderr)
        return EXIT_REFUSED


def _env_file_names(path: pathlib.Path) -> frozenset[str]:
    """.env に在る変数の**名前だけ**（値は読み捨てる。コメント行は除く）。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return frozenset()
    names = set()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            names.add(stripped.split("=", 1)[0].strip())
    return frozenset(names)


def _repo_alembic_head() -> str:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config(str(REPO / "alembic.ini"))
    config.set_main_option("script_location", str(REPO / "infrastructure" / "db" / "migrations"))
    heads = ScriptDirectory.from_config(config).get_heads()
    return ",".join(heads)


def _precheck(args, env, runner, now, objects) -> int:  # type: ignore[no-untyped-def]
    env_file = pathlib.Path(args.env_file or REPO / ".env")
    settings = Settings(_env_file=env_file)  # pyright: ignore[reportCallIssue]
    file_names = _env_file_names(env_file)

    def source(name: str) -> str:
        return (
            "environment"
            if name in env
            else "env-file"
            if name in file_names
            else "built-in default"
        )

    cfg = PrecheckConfig(
        db_root=args.db_root or env.get(ENV_DB_ROOT),
        object_root=args.object_root or env.get(ENV_OBJECT_ROOT),
        buckets=tuple(args.bucket or [settings.minio_bucket]),
        postgres_container=args.postgres_container,
        minio_container=args.minio_container,
        protected_paths=(str(MINIO_DATA_DIR), str(Path(settings.ai_video_work_root))),
        daily_cron=DEFAULT_DAILY_SCHEDULE_CRON,
        schedule_timezone=DEFAULT_SCHEDULE_TIMEZONE,
        watchdog_cron=DEFAULT_WATCHDOG_CRON,
        credential_sources={n: source(n) for n in REQUIRED_CREDENTIALS},
    )
    deps = PrecheckDeps(
        runner=runner,
        postgres=PostgresAccess(runner, args.postgres_container, args.pg_user),
        objects=objects or MinioObjectSource.from_settings(settings),
        fs=OsFs(),
        now=now,
        repo_alembic_head=_repo_alembic_head,
    )
    report = Prechecker(cfg, deps).run()
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if not args.json_only:
        print(render_human(report), file=sys.stderr)
    return EXIT_OK if report["verdict"] == "PASS" else EXIT_FAILED


def _run(args, env, db_root, object_root, runner, audit, notifier, now) -> int:  # type: ignore[no-untyped-def]
    settings = Settings(_env_file=pathlib.Path(args.env_file or REPO / ".env"))  # pyright: ignore[reportCallIssue]
    offsite = shlex.split(env[ENV_OFFSITE_CMD]) if env.get(ENV_OFFSITE_CMD) else None
    cfg = BackupConfig(
        db_root=db_root,
        object_root=object_root,
        buckets=tuple(args.bucket or [settings.minio_bucket]),
        git_revision=_git(["rev-parse", "HEAD"]) or "unknown",
        git_dirty=bool(_git(["status", "--porcelain"])),
        allow_same_device=args.allow_same_device,
        protected_paths=(str(MINIO_DATA_DIR), str(Path(settings.ai_video_work_root))),
        quiesce=args.quiesce_note,
        offsite_command=offsite,
        tool_versions={"python": sys.version.split()[0], "docker": _docker_version(runner)},
    )
    sources = {
        "db": lambda: _mount_device(runner, args.postgres_container, "/var/lib/postgresql/data"),
        "objects": lambda: _mount_device(runner, args.minio_container, "/data"),
    }
    cache: dict[str, int] = {}

    def source_device(role: str) -> int:
        if role not in cache:
            cache[role] = sources[role]()
        return cache[role]

    deps = BackupDeps(
        runner=runner,
        postgres=PostgresAccess(runner, args.postgres_container, args.pg_user),
        objects=MinioObjectSource.from_settings(settings),
        audit=audit,
        notifier=notifier,
        source_device=source_device,
        path_device=lambda p: os.stat(p).st_dev,
        now=now,
    )
    try:
        BackupService(cfg, deps).run()
    except BackupFailed:
        return EXIT_FAILED
    return EXIT_OK


def _docker_version(runner: CommandRunner) -> str:
    res = runner.run(["docker", "--version"])
    return res.stdout.strip() if res.returncode == 0 else "unknown"


def _drill(args, env, db_root, object_root, runner, audit, notifier, now) -> int:  # type: ignore[no-untyped-def]
    backup_id = args.backup_id
    if backup_id == "latest":
        ids = catalog.list_backup_ids(db_root)
        if not ids:
            raise UsageError("no backup to drill")
        backup_id = ids[-1]
    suffix = f"{backup_id.lower()}-{_free_suffix()}"
    name = f"{RESTORE_CONTAINER_PREFIX}{suffix}"
    settings = Settings(_env_file=pathlib.Path(args.env_file or REPO / ".env"))  # pyright: ignore[reportCallIssue]
    bucket = settings.minio_bucket
    target = RestoreTarget(
        container_name=name,
        compose_project=name,
        pg_host="docker-exec",
        pg_port=0,
        databases=tuple(restore_db_name(d) for d in BACKUP_DATABASES),
        buckets=(f"{RESTORE_BUCKET_PREFIX}{bucket}",),
        work_dir=str(Path(args.work_root).resolve() / name),
    )
    protected = (str(MINIO_DATA_DIR), str(Path(settings.ai_video_work_root)))
    deps = DrillDeps(runner, audit, notifier, now=now, protected_paths=protected)
    drill = RestoreDrill(db_root, object_root, target, deps)
    try:
        result = drill.run(backup_id)
    except DrillAborted as exc:
        audit.emit("restore.aborted", backup_id=backup_id, error=str(exc))
        print(f"aborted: {exc}", file=sys.stderr)
        return EXIT_FAILED
    return EXIT_OK if result["restore_verified"] else EXIT_FAILED


def _prune(args, env, db_root, object_root, runner, audit, notifier, now) -> int:  # type: ignore[no-untyped-def]
    policy = Policy(args.keep_daily, args.keep_weekly, args.keep_monthly)
    plan = prune(db_root, object_root, policy, audit, dry_run=not args.apply)
    print(
        json.dumps({"delete": plan.delete, "keep": plan.keep, "warnings": plan.warnings}, indent=2)
    )
    return EXIT_OK


def _verify(args, env, db_root, object_root, runner, audit, notifier, now) -> int:  # type: ignore[no-untyped-def]
    problems = verify_files(db_root, object_root, args.backup_id)
    audit.emit("backup.verify_files", backup_id=args.backup_id, problems=problems[:20])
    if problems:
        with contextlib.suppress(Exception):  # 通知の失敗で結果を変えない
            notifier.notify({"event": "backup.verify_failed", "backup_id": args.backup_id})
        print("\n".join(problems), file=sys.stderr)
        return EXIT_FAILED
    return EXIT_OK


def _status(args, env, db_root, object_root, runner, audit, notifier, now) -> int:  # type: ignore[no-untyped-def]
    others = () if db_root == object_root else (object_root,)
    entries = catalog.catalog(db_root, others)
    rows = [
        {
            "backup_id": e.backup_id,
            "created_at": e.created_at.isoformat(),
            "complete": e.complete,
            "restore_verified": e.restore_verified,
        }
        for e in entries
    ]
    print(json.dumps(rows, indent=2))
    fresh = [
        e
        for e in entries
        if e.complete and (now() - e.created_at).total_seconds() < args.max_age_hours * 3600
    ]
    if not fresh:
        audit.emit("backup.stale", max_age_hours=args.max_age_hours)
        return EXIT_FAILED
    return EXIT_OK
