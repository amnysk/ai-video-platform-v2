"""拒否されたシーンの代替案 Activity の名前と入出力（ADR-0035）。I/O を持たない。

workflow（``workers/production/workflows.py``）と Activity
（``workers/production/scene_recovery_activities.py``）の両方がここを import する。

TODO(ADR-0035): 他の production Activity と同じく ``contracts/production_activities.py`` へ
移す（このブランチでは contracts を編集しない分担のため、ここに1箇所だけ置いている）。

planner は Codex CLI を使うので、production worker（Codex を持たない）ではなく専用の worker
（``workers/production/scene_alternative/run_worker.py``）が専用の task queue で提供する
（storyboard worker と同じく Codex と認証がある環境で動かす）。worker が居なければ Activity は
schedule_to_close で timeout し、そのシーンは needs_input で止まる（自動では進まない側に倒れる）。
"""

from __future__ import annotations

from dataclasses import dataclass

#: Activity 名（workflow は名前で呼ぶ / INV-3）
PLAN_SCENE_ALTERNATIVE = "production_plan_scene_alternative"
#: planner を提供する worker の task queue（Codex を使う）
SCENE_ALTERNATIVE_TASK_QUEUE = "production-scene-alternative"
#: 既存の実行履歴の再生を壊さないための patch id
SCENE_ALTERNATIVE_PATCH_ID = "scene-alternative-recovery-v1"


@dataclass
class PlanSceneAlternativeRequest:
    episode_id: str
    workflow_id: str
    run_id: str
    scene_id: str
    storyboard_artifact_id: str
    #: この workflow 実行がすでに試した代替案の revision（無ければ 0）。現行の案がこれより
    #: 新しければそれを返す（Activity の再実行で二重に計画しない）。同じなら「同じ案のまま
    #: 再び止まった」ので計画せず needs_input にする（無限ループを作らない）。
    seen_revision: int = 0


@dataclass
class SceneAlternativeOutcome:
    """保存した代替案（``SCENE_VISUAL_OVERRIDE`` Artifact）。"""

    override_artifact_id: str
    revision: int
    visual_subject: str
    #: 今回新しく計画したか（既存の案を返しただけなら False）
    newly_planned: bool
