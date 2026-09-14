"""完成動画の入力指紋（ADR-0019 §5）。純粋関数のみ。

``render_input_hash`` の**構成要素の定義はここに1つだけ**置く（AGENTS.md §8）。
冪等キーは台本と同じ規則なので ``domain.script.identity.idempotency_key`` を再利用する。

含めない: 試行回数 / job_id / workflow run id / 時刻 / ファイルパス（フォント・バイナリの置き場）。
"""

from __future__ import annotations

from contracts.render import (
    RENDER_ARTIFACT_SCHEMA_VERSION,
    RenderEngineIdentity,
    RenderPlan,
    RenderProfile,
    TimelinePolicy,
)
from contracts.states import ArtifactType
from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from domain.render.audio import AudioMixSpec, audio_mix_spec
from domain.script.identity import idempotency_key

__all__ = ["idempotency_key", "render_input_hash", "render_plan_sha256"]


def render_input_hash(
    *,
    manifest_sha256: str,
    script_sha256: str,
    storyboard_sha256: str,
    profile: RenderProfile,
    policy: TimelinePolicy,
    engine: RenderEngineIdentity,
    font_sha256: str,
    audio_mix: AudioMixSpec | None = None,
) -> str:
    """完成動画の入力指紋。profile は字幕設定を含む全体を正準化して入れる。"""
    mix = audio_mix if audio_mix is not None else audio_mix_spec(profile)
    return sha256_hex(
        canonical_json_bytes(
            {
                "stage": "render",
                "artifact_type": ArtifactType.FINAL_VIDEO.value,
                "schema_version": RENDER_ARTIFACT_SCHEMA_VERSION,
                "manifest_sha256": manifest_sha256,
                "script_sha256": script_sha256,
                "storyboard_sha256": storyboard_sha256,
                "profile": profile.model_dump(mode="json"),
                "policy": policy.model_dump(mode="json"),
                "template_version": policy.template_version,
                "engine": engine.model_dump(mode="json"),
                "font_sha256": font_sha256,
                "audio_mix": mix.as_payload(),
            }
        )
    )


def render_plan_sha256(plan: RenderPlan) -> str:
    """描画計画の正準 JSON の sha256（``FinalVideoArtifact.render_plan_sha256``）。"""
    return sha256_hex(canonical_json_bytes(plan.model_dump(mode="json")))
