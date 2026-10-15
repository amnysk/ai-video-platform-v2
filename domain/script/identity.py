"""台本生成の入力指紋と冪等キー（ADR-0012 / ADR-0013）。純粋関数のみ。

``input_hash`` の**構成要素の定義はここに1つだけ**置く。
別モジュールで同じ材料を組み直さないこと（AGENTS.md §8）。
構成要素を変えると過去の Artifact と一致しなくなり全 Episode で再生成が走るので、
変更は ADR-0012 の陳腐化条件として扱う。
"""

from __future__ import annotations

from domain.artifact.hashing import canonical_json_bytes, sha256_hex

__all__ = ["idempotency_key", "script_input_hash", "with_brief_revision"]


def script_input_hash(
    *,
    episode_id: str,
    topic: str,
    artifact_type: str,
    target_schema_version: str,
    prompt_template_id: str,
    prompt_template_version: str,
    generator_id: str,
    locale: str,
    topic_plan_id: str | None,
    content_profile: str,
) -> str:
    """この成果物を作った**入力**の指紋。

    含める: episode_id / topic / artifact_type / 目標 schema_version /
    プロンプトテンプレートIDとバージョン / 生成器ID（provider + モデル）/
    locale・TopicPlan の id・content profile（``id@version``）（ADR-0026。locale や形式が
    違う台本を同じ Artifact として再利用しない。plan の題材の列は plan id が決める）。

    含めない: ラウンド番号 / 試行回数 / 時刻 / ホスト名 / job_id。
    これらを混ぜると「同じ入力なら呼ばない」判定が毎回外れ、
    Activity 再実行のたびに有料呼び出しが走る（ADR-0012 の事故そのもの）。
    """
    payload = {
        "episode_id": episode_id,
        "topic": topic,
        "artifact_type": artifact_type,
        "target_schema_version": target_schema_version,
        "prompt_template_id": prompt_template_id,
        "prompt_template_version": prompt_template_version,
        "generator_id": generator_id,
        "locale": locale,
        "topic_plan_id": topic_plan_id,
        "content_profile": content_profile,
    }
    return sha256_hex(canonical_json_bytes(payload))


def with_brief_revision(input_hash: str, brief_revision: str | None) -> str:
    """``script_input_hash`` に現行 CreativeBrief の内容 hash を畳み込む（agentic の Episode だけ）。

    brief が変わったら古い brief で作った台本を採用しない。``None``（plan のある / brief の無い
    Episode）は ``input_hash`` をそのまま返すので従来と同じ値（INV-55）。``script_input_hash`` の
    引数（構成要素の列）は変えない。brief を持つ旧い行は新しい hash と一致しないので、
    1度だけ作り直される（安全側。古い brief で作った台本かどうかは行から判別できない）。
    """
    if brief_revision is None:
        return input_hash
    return sha256_hex(f"{input_hash}|creative_brief:{brief_revision}".encode())


def idempotency_key(*, provider: str, input_hash: str, round: int) -> str:
    """予約台帳の UNIQUE キー。``provider | input_hash | round`` から導く。

    ラウンドは含める（2ラウンド目は別の呼び出しなので別予約になる）が、
    **Temporal の activity attempt を混ぜてはならない**。
    自動 retry ごとに別キーになると、同じラウンドで二重に課金する。
    """
    if round < 1:
        raise ValueError(f"round must be >= 1: {round}")
    return sha256_hex(f"{provider}|{input_hash}|{round}".encode())
