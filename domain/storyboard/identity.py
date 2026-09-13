"""storyboard 生成の入力指紋（ADR-0012 / ADR-0015）。純粋関数のみ。

``input_hash`` の**構成要素の定義はここに1つだけ**置く（AGENTS.md §8）。
冪等キーは台本と同じ規則なので ``domain.script.identity.idempotency_key`` を再利用する。
"""

from __future__ import annotations

from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from domain.script.identity import idempotency_key

__all__ = ["idempotency_key", "storyboard_input_hash"]


def storyboard_input_hash(
    *,
    episode_id: str,
    artifact_type: str,
    target_schema_version: str,
    script_sha256: str,
    prompt_template_id: str,
    prompt_template_version: str,
    generator_id: str,
    generation_spec_id: str,
) -> str:
    """この storyboard を作った**入力**の指紋。

    含める: episode_id / artifact_type / 目標 schema_version / 入力台本の sha256 /
    プロンプトテンプレートIDと版 / 生成器ID / 生成仕様ID。

    含めない: ラウンド番号 / 試行回数 / job_id / 時刻 / workflow run id。
    混ぜると再実行のたびに skip 判定が外れ、有料呼び出しが走る（ADR-0012）。
    """
    payload = {
        "episode_id": episode_id,
        "artifact_type": artifact_type,
        "target_schema_version": target_schema_version,
        "script_sha256": script_sha256,
        "prompt_template_id": prompt_template_id,
        "prompt_template_version": prompt_template_version,
        "generator_id": generator_id,
        "generation_spec_id": generation_spec_id,
    }
    return sha256_hex(canonical_json_bytes(payload))
