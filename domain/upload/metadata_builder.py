"""upload 工程と品質ゲートが共有する投稿メタデータの唯一の組み立て口（ADR-0081 §5 / INV-131）。

gate が lint するテキストと upload 工程が送るテキストは、この関数の結果であり同じ。
flag ``AVP_UPLOAD_DESCRIPTION_COMPOSED``（既定 OFF）が OFF のときは、従来の
``build_youtube_metadata(...)`` 呼び出しと引数も結果も同じ（``composition=None``）。
ON のときだけ台本から ``DescriptionComposition`` を作る。
環境変数は ``contracts.upload`` が読む。
"""

from __future__ import annotations

from collections.abc import Sequence

from contracts.artifacts import ScriptArtifact
from contracts.upload import (
    DescriptionComposition,
    DescriptionReference,
    YouTubeVideoMetadata,
    build_youtube_metadata,
    description_composition_enabled,
)


def build_description_composition(
    script: ScriptArtifact,
    *,
    credits: Sequence[str] = (),
    references: Sequence[DescriptionReference] = (),
    references_required: bool = False,
) -> DescriptionComposition:
    """台本から description の構造化入力を作る。

    導入文 = 台本の hook、補足 = ナレーション全体（入りきらない補足は ``compose_description`` が
    まるごと落とす）。クレジット（BGM の帰属文）・出典は呼び出し側が渡す（書き換えない）。
    """
    return DescriptionComposition(
        intro=script.hook,
        supplements=(script.narration,),
        references=tuple(references),
        references_required=references_required,
        credits=tuple(credits),
    )


def build_upload_metadata(
    *,
    upload_key: str,
    script: ScriptArtifact,
    credits: Sequence[str] = (),
    references: Sequence[DescriptionReference] = (),
    references_required: bool = False,
) -> YouTubeVideoMetadata:
    composition = (
        build_description_composition(
            script, credits=credits, references=references, references_required=references_required
        )
        if description_composition_enabled()
        else None
    )
    return build_youtube_metadata(
        upload_key=upload_key,
        title=script.title,
        hook=script.hook,
        narration=script.narration,
        language=script.language,
        composition=composition,
    )
