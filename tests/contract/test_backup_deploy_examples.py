"""定時実行の例は例のままで、既定は外部送信なし・自動 install なし（ADR-0051）。"""

from __future__ import annotations

import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[2]
DEPLOY = REPO / "deploy" / "backup"


def test_the_examples_exist_and_call_the_backup_script_only() -> None:
    names = {p.name for p in DEPLOY.iterdir()}
    assert {
        "README.md",
        "avp-backup.env.example",
        "avp-backup.service",
        "avp-backup.timer",
        "avp-backup-drill.service",
        "avp-backup-drill.timer",
        "crontab.example",
    } <= names
    for name in ("avp-backup.service", "avp-backup-drill.service"):
        text = (DEPLOY / name).read_text(encoding="utf-8")
        execs = re.findall(r"^ExecStart=(.*)$", text, re.MULTILINE)
        assert execs and all("scripts/backup.py" in e for e in execs), name
        assert not re.search(r"docker (compose|restart|rm|stop|up|down)", text)


def test_the_default_env_has_no_destination_no_notification_no_offsite() -> None:
    lines = (DEPLOY / "avp-backup.env.example").read_text(encoding="utf-8").splitlines()
    active = [x for x in lines if x.strip() and not x.lstrip().startswith("#")]
    assert active == ["AVP_BACKUP_DB_ROOT=", "AVP_BACKUP_OBJECT_ROOT="]


def test_nothing_installs_or_references_the_examples() -> None:
    for name in ("Makefile", "Dockerfile", "compose.yaml", "compose.staging.yaml"):
        text = (REPO / name).read_text(encoding="utf-8")
        assert "deploy/backup" not in text and "backup.py" not in text, name
    for p in DEPLOY.iterdir():
        text = p.read_text(encoding="utf-8")
        assert not re.search(r"systemctl|crontab -|enable --now", text.replace("# 例", "")), p.name
    for script in (REPO / "scripts").glob("*.sh"):
        assert "backup.py" not in script.read_text(encoding="utf-8"), script.name
