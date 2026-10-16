"""台本の Evidence 照合（ADR-0038 §B6。opt-in・既定 OFF）の Workflow 側の語彙。

``ScriptWorkflow`` が import するのは**このモジュールだけ**（Research のコードを import しない。
実装は ``workers/planning/script_evidence_activities.py``、組み立ては ``research_wiring.py``）。

- ``SCRIPT_EVIDENCE_PATCH_ID``: Evidence の分岐を履歴に記録する patch id。OFF の worker の新しい
  実行は ``workflow.patched`` を呼ばないので、履歴は接続前（f209e7c）と同じ
- ``SCRIPT_EVIDENCE_CHECK``: 1 本の Activity（依頼 → 上限つきで待つ → 照合 → 記録）。
  結果は**助言**で、どの結果でも・Activity が失敗しても Workflow は ``mark_script_ready``
  へ進む（INV-37）
- 自動の台本書き直し（Codex の追加呼び出し）は移植していない（ADR-0038 §B6）
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum

from temporalio.common import RetryPolicy

__all__ = [
    "SCRIPT_EVIDENCE_CHECK",
    "SCRIPT_EVIDENCE_HEARTBEAT_TIMEOUT",
    "SCRIPT_EVIDENCE_PATCH_ID",
    "SCRIPT_EVIDENCE_POLL_SECONDS",
    "SCRIPT_EVIDENCE_RETRY_POLICY",
    "SCRIPT_EVIDENCE_START_TO_CLOSE",
    "SCRIPT_EVIDENCE_WAIT_SECONDS",
    "EvidenceCheckOutcome",
    "ScriptEvidenceOutcome",
    "ScriptEvidenceRequest",
]

SCRIPT_EVIDENCE_PATCH_ID = "script-evidence-b6"
SCRIPT_EVIDENCE_CHECK = "script_evidence_check"

#: Research の完了を待つ上限（秒）。超えたら「調査なし」で台本工程を終える（Research は走り続ける）
SCRIPT_EVIDENCE_WAIT_SECONDS = 15 * 60
#: 状態を読み直す間隔（秒）。DB を読むだけ
SCRIPT_EVIDENCE_POLL_SECONDS = 10
#: Activity の最長時間 = 待つ上限 + 依頼と照合の余裕
SCRIPT_EVIDENCE_START_TO_CLOSE = timedelta(seconds=SCRIPT_EVIDENCE_WAIT_SECONDS) + timedelta(
    minutes=5
)
#: worker が落ちたことを知る時間（待つ間は heartbeat を送る）
SCRIPT_EVIDENCE_HEARTBEAT_TIMEOUT = timedelta(minutes=2)
#: 依頼は冪等キーで同じ依頼に戻るので再実行してよい。回数は小さく（助言なので諦めて進む）
SCRIPT_EVIDENCE_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=2,
)


class EvidenceCheckOutcome(StrEnum):
    """照合の段の結果。どれでも Episode は進む（助言）。"""

    #: Evidence が ``completed`` で、照合結果を記録した（結論は ``verdict``）
    VERIFIED = "verified"
    #: Evidence が ``completed`` でない（``blocked`` / ``partial`` / ``failed``）=「調査なし」
    NO_RESEARCH = "no_research"
    #: 待つ上限を越えた =「調査なし」（Research は止めない）
    TIMEOUT = "timeout"
    #: 台本に検証可能な主張の候補が無い（依頼しない）
    NO_CLAIMS = "no_claims"
    #: 照合の段で例外（型名だけを ``reason`` に残す）
    ERROR = "error"


@dataclass
class ScriptEvidenceRequest:
    episode_id: str
    #: 照合する台本（本番の Artifact。参照だけで、中身は読み戻して sha256 を照合してから使う）
    artifact_id: str
    object_key: str
    sha256: str


@dataclass
class ScriptEvidenceOutcome:
    outcome: str
    research_request_id: str | None = None
    #: Evidence の依頼の状態（``contracts.research.ResearchStatus``）
    research_status: str | None = None
    #: 照合の結論（``contracts.research_evidence.VerificationVerdict``）
    verdict: str | None = None
    #: 照合結果（research の成果物。Evidence の依頼が所有する）
    verification_artifact_id: str | None = None
    verification_sha256: str | None = None
    #: 理由（コード・型名だけ。例外文は写さない / INV-20）
    reason: str | None = None
