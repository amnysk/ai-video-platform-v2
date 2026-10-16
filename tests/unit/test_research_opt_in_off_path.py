"""Research への opt-in 接続は既定 OFF で、OFF のとき接続前（f209e7c）と同じ（INV-37、ADR-0038/0039
§B6）。

守るもの（OFF 側。ON の挙動は ``test_planner_trend_opt_in.py`` / ``test_script_evidence_opt_in.py``
/ ``test_script_evidence_workflow_opt_in.py``）:
- 設定の既定は OFF。compose の script-worker も既定 OFF で渡す
- OFF の planning worker は Research のコードを import せず、登録する workflow・Activity は接続前と
  同じ（``ScriptWorkflow`` と ``TopicPlannerWorkflow``。Evidence の Activity は登録しない）
- Topic の prompt テンプレートと版、台本の同一性（``script_input_hash``）は f209e7c と同じ値
  （golden は ``git archive f209e7c`` を展開した木で計算した）
- ON でも ``YOUTUBE_CHANNEL_ID`` が無ければ接続しない（OFF と同じ）

理由は docs/testing/research-opt-in-rationale.md。
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from domain.script.identity import script_input_hash
from infrastructure.config import Settings
from infrastructure.storage.memory_store import InMemoryArtifactStore
from prompts import (
    TOPIC_PROMPT_TEMPLATE_ID,
    TOPIC_PROMPT_VERSION,
    load_prompt_template,
)
from workers.planning import run_worker
from workers.planning.script_evidence import SCRIPT_EVIDENCE_CHECK
from workers.planning.topic_workflows import TopicPlannerWorkflow
from workers.planning.workflows import EvidenceScriptWorkflow, ScriptWorkflow

ROOT = Path(__file__).resolve().parents[2]
#: f209e7c の ``prompts/topic_en.md`` の sha256（``git show f209e7c:prompts/topic_en.md |
#: sha256sum``）
F209E7C_TOPIC_TEMPLATE_SHA256 = "975c22e7560f137166948da3d8585c9c0c8e3aa71ff5f38358f07bbc8018d8a9"
#: f209e7c の ``domain/script/identity.py`` で同じ引数から計算した ``script_input_hash``
F209E7C_SCRIPT_INPUT_HASH = "d7fe8c115607b2dcb238f9072d5f3f9162f96df0f720ad2f31175bf95ed9e836"
FLAGS = ("PLANNER_TREND_ENABLED", "SCRIPT_EVIDENCE_ENABLED", "YOUTUBE_CHANNEL_ID")


def _settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    for key in FLAGS:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)  # pyright: ignore[reportCallIssue]


def _registrations(settings: Settings, session_factory) -> run_worker.ResearchRegistrations:
    return run_worker.research_registrations(
        settings,
        client=None,
        session_factory=session_factory,
        store=InMemoryArtifactStore(),
        clock=lambda: datetime.now(UTC),
    )


def test_the_opt_in_settings_default_to_off() -> None:
    assert Settings.model_fields["planner_trend_enabled"].default is False
    assert Settings.model_fields["script_evidence_enabled"].default is False


def test_off_registers_exactly_what_the_worker_registered_before(
    monkeypatch: pytest.MonkeyPatch, session_factory
) -> None:
    registrations = _registrations(_settings(monkeypatch), session_factory)
    assert registrations.trend is None
    assert registrations.activities == []
    assert registrations.workflows == run_worker.WORKFLOWS == [ScriptWorkflow, TopicPlannerWorkflow]


def test_the_off_worker_does_not_import_research_code() -> None:
    """OFF の組み立ては Research の実装（Gateway・起動・照合・Trend の参照）を読み込まない。"""
    code = """
import sys
from datetime import UTC, datetime
from infrastructure.config import Settings
from infrastructure.storage.memory_store import InMemoryArtifactStore
from workers.planning import run_worker
run_worker.research_registrations(
    Settings(_env_file=None), client=None, session_factory=None,
    store=InMemoryArtifactStore(), clock=lambda: datetime.now(UTC),
)
loaded = sorted(
    m for m in sys.modules
    if m.startswith(("infrastructure.research", "workers.research", "domain.research"))
    or m in {"infrastructure.temporal.research_starter", "workers.planning.research_wiring",
             "workers.planning.topic_trend", "workers.planning.script_evidence_activities"}
)
print(loaded)
assert loaded == [], loaded
"""
    env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(ROOT)}
    done = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True
    )
    assert done.returncode == 0, done.stdout + done.stderr


@pytest.mark.parametrize(
    "flag", ["PLANNER_TREND_ENABLED", "SCRIPT_EVIDENCE_ENABLED"], ids=["trend", "evidence"]
)
def test_on_without_a_channel_id_stays_off(
    monkeypatch: pytest.MonkeyPatch, session_factory, flag: str
) -> None:
    registrations = _registrations(_settings(monkeypatch, **{flag: "true"}), session_factory)
    assert registrations.trend is None and registrations.activities == []
    assert registrations.workflows == [ScriptWorkflow, TopicPlannerWorkflow]


def test_on_registers_the_evidence_workflow_and_activity(
    monkeypatch: pytest.MonkeyPatch, session_factory
) -> None:
    class _Starter:
        async def start_research(self, *, request_id: str) -> str:
            return request_id

    from workers.planning import research_wiring

    settings = _settings(
        monkeypatch,
        PLANNER_TREND_ENABLED="true",
        SCRIPT_EVIDENCE_ENABLED="true",
        YOUTUBE_CHANNEL_ID="UC0123456789012345678901",
    )
    original = research_wiring.build_research_links
    monkeypatch.setattr(
        research_wiring,
        "build_research_links",
        lambda *a, **kw: original(*a, **{**kw, "starter": _Starter()}),
    )
    registrations = _registrations(settings, session_factory)
    assert registrations.trend is not None
    assert registrations.workflows == [EvidenceScriptWorkflow, TopicPlannerWorkflow]
    names = [fn.__temporal_activity_definition.name for fn in registrations.activities]  # type: ignore[attr-defined]
    assert names == [SCRIPT_EVIDENCE_CHECK]


def test_the_topic_template_and_version_are_those_of_f209e7c() -> None:
    template = load_prompt_template(TOPIC_PROMPT_TEMPLATE_ID)
    assert hashlib.sha256(template.encode("utf-8")).hexdigest() == F209E7C_TOPIC_TEMPLATE_SHA256
    assert TOPIC_PROMPT_VERSION == "topic_en@2"


def test_the_script_identity_is_that_of_f209e7c() -> None:
    assert (
        script_input_hash(
            episode_id="e1",
            topic="t",
            artifact_type="script",
            target_schema_version="1.0",
            prompt_template_id="script/en-US",
            prompt_template_version="1",
            generator_id="codex:m",
            locale="en-US",
            topic_plan_id="p1",
            content_profile="shorts@1",
        )
        == F209E7C_SCRIPT_INPUT_HASH
    )


def test_compose_passes_the_opt_in_flags_to_the_script_worker_off_by_default() -> None:
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    for name, service in compose["services"].items():
        env = service.get("environment") or {}
        having = {k for k in env if k in {"PLANNER_TREND_ENABLED", "SCRIPT_EVIDENCE_ENABLED"}}
        if name == "script-worker":
            assert env["PLANNER_TREND_ENABLED"] == "${PLANNER_TREND_ENABLED:-false}"
            assert env["SCRIPT_EVIDENCE_ENABLED"] == "${SCRIPT_EVIDENCE_ENABLED:-false}"
        else:
            assert not having, name
