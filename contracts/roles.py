"""実行役の契約 RoleSpec（JP-MAS-001 §3 / §13、ADR-0041）。

役割は「保存された入力を読み、schema に従った成果物・提案を返す」だけで、
共有 DB を自由に書き換えない。
採用する権限は役割ではなく運転ポリシーと command の権限で決まる。

RoleSpec は**ソース管理された設定**であり、任意コードをダウンロードして実行する plugin では
ない（設計 §13）。
役割を足すときはここへ RoleSpec を足し、``tests/contract/test_role_specs.py`` を通す。
"""

from __future__ import annotations

from dataclasses import dataclass

from contracts.agents import TERMINATION, AgentTool
from contracts.intervention import AgentRole


@dataclass(frozen=True, slots=True)
class RoleSpec:
    role: AgentRole
    #: どの既存処理を包むか（実体のある Activity・adapter。名前の付け替えではない）
    implemented_by: tuple[str, ...]
    #: 読む入力（成果物の型・表）
    inputs: tuple[str, ...]
    #: 出す成果物・記録
    outputs: tuple[str, ...]
    #: 許可する tool（外部呼び出しの種別）。これ以外を呼ぶ経路を持たない
    allowed_tools: tuple[str, ...]
    #: 予算（1回の実行で使える外部呼び出しの上限。課金の正本は予約台帳）
    max_llm_calls_per_run: int
    #: 終了条件
    termination: tuple[str, ...]
    #: 書き込みの権限: 成果物の新 revision を作れるか（False なら提案・所見だけ）
    may_create_revisions: bool


PRODUCER = RoleSpec(
    role=AgentRole.PRODUCER,
    implemented_by=("workers.planning.topic_activities (TopicPlannerWorkflow)",),
    inputs=(
        "topic_plans / episodes (Content Memory)",
        "analytics_snapshots",
        "research_trend (opt-in)",
    ),
    outputs=("topic_plans", "topic_candidates", "decision_records(choose_topic)", "agent_runs"),
    allowed_tools=("llm:codex_cli", "youtube_analytics:read"),
    max_llm_calls_per_run=3,
    termination=(
        "TopicPlan が selected になった",
        "全候補が却下され round 上限に達した（plan を作らず Daily が失敗する / INV-21）",
    ),
    may_create_revisions=True,
)

WRITER_DIRECTOR = RoleSpec(
    role=AgentRole.WRITER_DIRECTOR,
    implemented_by=(
        "workers.planning.activities (ScriptWorkflow)",
        "workers.storyboard.activities (StoryboardWorkflow)",
        "workers.production.scene_recovery_activities (scene alternative)",
    ),
    inputs=(
        "topic_plans.topic",
        "script (current / adopted)",
        "user_constraints",
        "research_evidence (opt-in)",
    ),
    outputs=(
        "script",
        "storyboard",
        "scene_visual_override",
        "decision_records",
        "agent_runs",
        "change_sets(提案)",
    ),
    allowed_tools=("llm:codex_cli",),
    max_llm_calls_per_run=3,
    termination=(
        "schema 検証を通った成果物を保存した",
        "LLM の形式不正が retry 上限に達した（retryable → blocked / ADR-0014）",
        "user_constraint に抵触する案は採用せず提案として残した",
    ),
    may_create_revisions=True,
)

REVIEWER = RoleSpec(
    role=AgentRole.REVIEWER,
    implemented_by=("infrastructure.intervention.reviewer (独立した入力で manifest を検査)",),
    inputs=(
        "adopted manifest",
        "script",
        "storyboard",
        "final_video.technical_qa",
        "research_script_verification",
    ),
    outputs=("annotations(review_finding)", "decision_records(review_verdict)", "agent_runs"),
    allowed_tools=("artifact:read",),
    max_llm_calls_per_run=0,
    termination=("採用 manifest の全検査項目に所見を出した",),
    #: レビュアーは成果物を作り替えない（所見と提案だけ）
    may_create_revisions=False,
)

# ADR-0043: 自律改善型制作の4役。実体の handler は P1 で ``domain.agents.handlers`` に実装する。
# allowed_tools・終了条件は contracts/agents.py の AgentSpec v1.0 と同じ値を使う（再掲しない）。
_LLM = AgentTool.LLM_GENERATE_JSON.value
_READ = AgentTool.ARTIFACT_READ.value
_ANALYTICS = AgentTool.ANALYTICS_READ.value
_PROPOSE = AgentTool.INTERVENTION_PROPOSE.value

STRATEGY = RoleSpec(
    role=AgentRole.STRATEGY,
    implemented_by=("domain.agents.handlers (strategy_agent_v1)",),
    inputs=("user_brief", "analytics_snapshots", "lesson", "episodes (既存動画一覧)"),
    outputs=("strategy_brief", "decision_records", "agent_runs"),
    allowed_tools=(_LLM, _READ, _ANALYTICS),
    max_llm_calls_per_run=2,
    termination=TERMINATION[AgentRole.STRATEGY],
    may_create_revisions=True,
)

CREATIVE_DIRECTOR = RoleSpec(
    role=AgentRole.CREATIVE_DIRECTOR,
    implemented_by=("domain.agents.handlers (creative_director_v1)",),
    inputs=("strategy_brief", "decision_records(choose_topic)", "user_constraints"),
    outputs=("creative_brief", "decision_records", "agent_runs"),
    allowed_tools=(_LLM, _READ, _PROPOSE),
    max_llm_calls_per_run=2,
    termination=TERMINATION[AgentRole.CREATIVE_DIRECTOR],
    may_create_revisions=True,
)

QUALITY_REVIEWER = RoleSpec(
    role=AgentRole.QUALITY_REVIEWER,
    implemented_by=("domain.agents.handlers (quality_reviewer_v1)",),
    inputs=(
        "adopted manifest",
        "script",
        "storyboard",
        "final_video.technical_qa",
        "reviewer 所見",
    ),
    outputs=("quality_review", "annotations(review_finding)", "decision_records", "agent_runs"),
    allowed_tools=(_LLM, _READ, _PROPOSE),
    max_llm_calls_per_run=2,
    termination=TERMINATION[AgentRole.QUALITY_REVIEWER],
    #: 自分の QualityReview は所見であり、制作成果物の revision ではない（修正は提案だけ）
    may_create_revisions=False,
)

LEARNING = RoleSpec(
    role=AgentRole.LEARNING,
    implemented_by=("domain.agents.handlers (learning_agent_v1)",),
    inputs=("upload_receipt", "analytics_snapshots", "adopted manifest", "strategy_brief"),
    outputs=("lesson", "decision_records", "agent_runs"),
    allowed_tools=(_LLM, _READ, _ANALYTICS),
    max_llm_calls_per_run=1,
    termination=TERMINATION[AgentRole.LEARNING],
    #: Lesson は仮説であり、制作成果物の revision を作らない
    may_create_revisions=False,
)

ROLE_SPECS: dict[AgentRole, RoleSpec] = {
    spec.role: spec
    for spec in (
        PRODUCER,
        WRITER_DIRECTOR,
        REVIEWER,
        STRATEGY,
        CREATIVE_DIRECTOR,
        QUALITY_REVIEWER,
        LEARNING,
    )
}
