"""Artifactのスキーマ定義（INV-10）。生成側と取り込み側がここを共有する。"""

from __future__ import annotations

import json
import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from contracts.states import ArtifactType

DUMMY_ARTIFACT_SCHEMA_VERSION = "1.0"
DUMMY_ARTIFACT_MESSAGE = "workflow completed"

SCRIPT_ARTIFACT_SCHEMA_VERSION = "1.0"
SCRIPT_MIN_SCENES = 3
SCRIPT_MAX_SCENES = 8
SCRIPT_MIN_TOTAL_DURATION_MS = 15_000
SCRIPT_MAX_TOTAL_DURATION_MS = 60_000  # YouTube Shorts の上限

#: シーン1件あたりの尺の範囲（ミリ秒）。
SCRIPT_MIN_SCENE_DURATION_MS = 1_000
SCRIPT_MAX_SCENE_DURATION_MS = 20_000

#: シーンIDの語彙。``s1`` .. ``s99``。
SCRIPT_SCENE_ID_PATTERN = r"^s[0-9]{1,2}$"

#: ナレーションを連結するときの区切り。導出値の定義を1箇所に置く。
SCRIPT_NARRATION_JOINER = " "


class DummyArtifact(BaseModel):
    """Phase 1 の骨組み用ダミー成果物。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    episode_id: str
    type: Literal[ArtifactType.DUMMY]
    schema_version: Literal["1.0"]
    message: str


class ScriptScene(BaseModel):
    """台本の1シーン。

    ``duration_ms`` は **int（ミリ秒）**。秒の float を持たないのは、浮動小数の
    表現差で正準JSONのバイト列が割れ、同じ内容が違う SHA-256 になるのを
    構造的に防ぐため。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=SCRIPT_SCENE_ID_PATTERN)
    narration: str = Field(min_length=1, max_length=220)
    visual: str = Field(min_length=1, max_length=400)
    duration_ms: int = Field(ge=SCRIPT_MIN_SCENE_DURATION_MS, le=SCRIPT_MAX_SCENE_DURATION_MS)


class ScriptMetadata(BaseModel):
    """台本の来歴。どの生成器のどのモデルが書いたかを成果物自身に残す。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    topic: str = Field(min_length=1, max_length=200)
    generator: str = Field(min_length=1, max_length=64)
    generator_model: str = Field(min_length=1, max_length=64)


class ScriptArtifact(BaseModel):
    """台本成果物。

    トップレベルに ``narration`` / ``total_duration_ms`` の**フィールドを置かない**。
    どちらも ``scenes`` から一意に決まるので、保存すると同じ真実が2箇所に散り、
    片方だけ更新された成果物を作れてしまう（AGENTS.md §8）。導出は property。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    episode_id: str = Field(min_length=1, max_length=64)
    type: Literal[ArtifactType.SCRIPT]
    schema_version: Literal["1.0"]
    # 既定値を置かない。言語は必ず生成側が宣言する（黙って ja になるのを防ぐ）。
    language: Literal["ja", "en"]
    title: str = Field(min_length=1, max_length=100)
    hook: str = Field(min_length=1, max_length=80)
    scenes: Annotated[
        tuple[ScriptScene, ...],
        Field(min_length=SCRIPT_MIN_SCENES, max_length=SCRIPT_MAX_SCENES),
    ]
    metadata: ScriptMetadata

    @property
    def total_duration_ms(self) -> int:
        """総尺。``scenes`` から導出する（保存しない）。"""
        return sum(scene.duration_ms for scene in self.scenes)

    @property
    def narration(self) -> str:
        """全ナレーション。``scenes[].narration`` から導出する（保存しない）。"""
        return SCRIPT_NARRATION_JOINER.join(scene.narration for scene in self.scenes)

    @model_validator(mode="after")
    def _check_scene_invariants(self) -> ScriptArtifact:
        seen: set[str] = set()
        for scene in self.scenes:
            if scene.id in seen:
                raise ValueError(f"duplicate scene id: {scene.id}")
            seen.add(scene.id)

        total = self.total_duration_ms
        if not (SCRIPT_MIN_TOTAL_DURATION_MS <= total <= SCRIPT_MAX_TOTAL_DURATION_MS):
            raise ValueError(
                "total duration must be within "
                f"{SCRIPT_MIN_TOTAL_DURATION_MS}..{SCRIPT_MAX_TOTAL_DURATION_MS} ms, got {total}"
            )
        return self


#: ArtifactType -> モデル。``parse_artifact`` のディスパッチ表。
#: 新しい ArtifactType を足したらここにも登録する
#: （``test_every_artifact_type_has_a_registered_model`` が強制する）。
ARTIFACT_MODELS: dict[ArtifactType, type[BaseModel]] = {
    ArtifactType.DUMMY: DummyArtifact,
    ArtifactType.SCRIPT: ScriptArtifact,
}


def build_dummy_artifact(*, episode_id: str) -> dict[str, Any]:
    """生成側。ここが返す形だけが正。"""
    return {
        "episode_id": episode_id,
        "type": ArtifactType.DUMMY.value,
        "schema_version": DUMMY_ARTIFACT_SCHEMA_VERSION,
        "message": DUMMY_ARTIFACT_MESSAGE,
    }


def build_script_artifact(
    *,
    episode_id: str,
    language: str,
    title: str,
    hook: str,
    scenes: Any,
    metadata: Any,
) -> dict[str, Any]:
    """生成側。**build の時点で検証を通してから** dict を返す。

    ``build_dummy_artifact`` は生 dict を返すが、Script は意図的に異なる。
    ダミーは値がこの関数の中で閉じているのに対し、台本の中身は外部生成器
    （LLM）由来で、ここが不正な成果物を作れる唯一の入口になるため。
    """
    artifact = ScriptArtifact.model_validate(
        {
            "episode_id": episode_id,
            "type": ArtifactType.SCRIPT.value,
            "schema_version": SCRIPT_ARTIFACT_SCHEMA_VERSION,
            "language": language,
            "title": title,
            "hook": hook,
            "scenes": scenes,
            "metadata": metadata,
        }
    )
    return artifact.model_dump(mode="json")


def parse_script_artifact(payload: dict[str, Any]) -> ScriptArtifact:
    """取り込み側。想定外の schema_version は推測せず ValidationError にする。"""
    return ScriptArtifact.model_validate(payload)


def parse_artifact(payload: dict[str, Any]) -> DummyArtifact | ScriptArtifact:
    """取り込み側の唯一の入口。payload の ``type`` でディスパッチする。

    未知の type・type 欠落は推測せず ``ValueError``。「読めそうな型で試す」を
    しないのは、間違った型で通ってしまう成果物を作らないため（INV-10）。
    """
    raw_type = payload.get("type")
    if raw_type is None:
        raise ValueError("artifact payload has no 'type'")
    try:
        artifact_type = ArtifactType(raw_type)
    except ValueError as exc:
        raise ValueError(f"unknown artifact type: {raw_type!r}") from exc
    model = ARTIFACT_MODELS[artifact_type]
    return model.model_validate(payload)  # type: ignore[return-value]


_CODE_FENCE_RE = re.compile(r"^\s*```(?:[A-Za-z0-9_+-]*)\s*\n(?P<body>.*?)\n?\s*```\s*$", re.DOTALL)


def extract_json_object(raw: str) -> dict[str, Any]:
    """生成器の生出力から JSON オブジェクトを取り出す。

    手順は決定的: フェンス除去 → 最初の ``{`` から最後の ``}`` を切り出す →
    ``json.loads``。**修復はしない**（截断JSONの閉じ括弧補完など）。推測して
    読まない（ADR-0014）。

    失敗時は ``ValueError`` を送出する。``contracts/`` は ``domain/`` を import
    できない（INV-6）ので、ここで ``ScriptOutputUnparseableError`` などの
    ドメイン例外へ投げ分けることはできない。**この ValueError をドメイン例外へ
    変換するのは呼び出し側（worker / adapter）の責務**。
    """
    text = raw.strip()
    fenced = _CODE_FENCE_RE.match(text)
    if fenced is not None:
        text = fenced.group("body").strip()

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no JSON object found in generator output")

    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"generator output is not valid JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise ValueError(f"expected a JSON object, got {type(parsed).__name__}")
    return parsed
