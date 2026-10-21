"""backup / restore の境界（ADR-0051 / INV-66）。

本番へは読み取りだけ、復旧は隔離先だけ、``restore_verified`` を True にできるのは drill だけ、
外部送信は既定オフ。書き込み系の動詞・コマンドが増えたら落ちる。
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

from infrastructure.backup.audit import CommandNotifier, NullNotifier, notifier_from_env
from infrastructure.backup.postgres import ROW_COUNT_SQL

REPO = pathlib.Path(__file__).resolve().parents[2]
BACKUP = REPO / "infrastructure" / "backup"
SCRIPT = REPO / "scripts" / "backup.py"
FORBIDDEN_SQL = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|GRANT|REVOKE|VACUUM)\b", re.IGNORECASE
)


def _text(name: str) -> str:
    return (BACKUP / name).read_text(encoding="utf-8")


def _string_constants(path: pathlib.Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]


def test_production_postgres_access_is_dump_and_select_only() -> None:
    """postgres.py が本番コンテナへ送る唯一の場所: pg_dump 系・psql(SELECT)・pg_restore --list。"""
    text = _text("postgres.py")
    tools = set(re.findall(r'"(psql|pg_dump|pg_dumpall|pg_restore|createdb|dropdb|pg_ctl)"', text))
    assert tools == {"psql", "pg_dump", "pg_dumpall", "pg_restore"}, tools
    assert '"--list"' in text  # pg_restore は目次の確認だけ（-d を渡さない）
    assert FORBIDDEN_SQL.search(ROW_COUNT_SQL) is None
    assert re.sub(r"\s+", " ", ROW_COUNT_SQL).startswith("SELECT ")
    # PostgresAccess を呼ぶ側が書く SQL も SELECT だけ
    for name in ("service.py", "restore.py", "cli.py"):
        for s in _string_constants(BACKUP / name):
            if re.match(
                r"^\s*(SELECT\s|INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|DROP\s|CREATE\s)",
                s,
                re.IGNORECASE,
            ):
                assert s.lstrip().upper().startswith("SELECT"), (name, s)


def test_only_the_restore_drill_runs_docker_run_rm_or_createdb() -> None:
    for path in [*BACKUP.glob("*.py"), SCRIPT]:
        if path.name == "restore.py":
            continue
        text = path.read_text(encoding="utf-8")
        assert "createdb" not in text and "dropdb" not in text, path.name
        assert not re.search(r'"docker",\s*"(run|rm|stop|kill|restart|up|down|volume|cp)"', text)


def test_the_cli_only_inspects_production_containers() -> None:
    text = (BACKUP / "cli.py").read_text(encoding="utf-8")
    docker_calls = set(re.findall(r'\["docker", "([a-z-]+)"', text))
    assert docker_calls <= {"inspect", "--version"}, docker_calls


def test_restore_never_names_a_production_container_and_uses_its_own_target() -> None:
    text = _text("restore.py")
    assert "PRODUCTION_" not in text and "avp2" not in text
    assert "assert_isolated(" in text
    # 拒否判定が最初の docker 呼び出しより前にある
    assert text.index("assert_isolated(") < text.index("self._docker(")
    assert '"--network", "none"' in re.sub(r"\s+", " ", text).replace('" , "', '", "')


def test_only_the_domain_drill_function_can_make_a_backup_verified() -> None:
    allowed = REPO / "domain" / "backup" / "manifest.py"
    pattern = re.compile(r"restore_verified[\"']?\]?\s*(=|:)\s*True|restore_verified=True")
    offenders = []
    for root in ("domain", "infrastructure", "apps", "workers", "scripts"):
        for path in (REPO / root).rglob("*.py"):
            if path != allowed and pattern.search(path.read_text(encoding="utf-8")):
                offenders.append(str(path.relative_to(REPO)))
    assert not offenders, offenders
    manifest = allowed.read_text(encoding="utf-8")
    assert manifest.count('"restore_verified": False') == 1  # new_manifest
    assert 'out["restore_verified"] = not problems' in manifest


def test_deleting_backups_is_confined_to_three_places() -> None:
    users = sorted(p.name for p in BACKUP.glob("*.py") if "rmtree" in p.read_text(encoding="utf-8"))
    # catalog: 検証済みの集合の削除 / service: 自分の *.partial / restore: 自分の作業ディレクトリ
    assert users == ["catalog.py", "restore.py", "service.py"], users
    assert "is_backup_id" in _text("catalog.py") and "is_symlink" in _text("catalog.py")


def test_network_calls_exist_only_in_the_notifier() -> None:
    """HTTP は audit.py の webhook だけ（objects.py は minio SDK 経由。urllib は URL 解析のみ）。"""
    names = {"httpx", "requests", "socket", "smtplib"}
    found: dict[str, set[str]] = {}
    for path in BACKUP.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        mods = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                mods |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom) and n.module:
                mods.add(n.module.split(".")[0])
        if mods & names:
            found[path.name] = mods & names
    assert found == {}, found
    assert "urllib.request" not in "".join(
        p.read_text(encoding="utf-8") for p in BACKUP.glob("*.py") if p.name != "audit.py"
    )


def test_notification_and_offsite_are_off_unless_configured() -> None:
    assert isinstance(notifier_from_env({}), NullNotifier)
    assert not isinstance(notifier_from_env({"AVP_BACKUP_NOTIFY_CMD": "true"}), NullNotifier)
    cli = _text("cli.py")
    assert "if env.get(ENV_OFFSITE_CMD) else None" in cli  # 未設定なら offsite は None


def test_command_notifier_passes_the_event_on_stdin(tmp_path: pathlib.Path) -> None:
    out = tmp_path / "event.json"
    CommandNotifier(f"sh -c 'cat > {out}'").notify({"event": "backup.failed", "backup_id": "x"})
    assert '"backup.failed"' in out.read_text()


def test_a_webhook_must_be_http_and_a_failing_target_is_reported_after_trying_all(
    tmp_path: pathlib.Path,
) -> None:
    with pytest.raises(ValueError):
        notifier_from_env({"AVP_BACKUP_NOTIFY_WEBHOOK": "file:///etc/passwd"})
    out = tmp_path / "e.json"
    n = notifier_from_env(
        {
            "AVP_BACKUP_NOTIFY_CMD": f"sh -c 'cat > {out}'",
            "AVP_BACKUP_NOTIFY_WEBHOOK": "http://127.0.0.1:9/hook",  # 閉じたポート
        }
    )
    with pytest.raises(RuntimeError):
        n.notify({"event": "e"})
    assert out.exists()  # webhook が落ちても command には届く
