"""Upload の契約（ADR-0020）: YouTube 投稿メタデータ・投稿マーカー・投稿受領 Artifact。

- 公開範囲は **private だけ**を型で表す（``Literal["private"]``。INV-19）
- 受領 Artifact に secret・resumable session URI・OAuth トークンの欄を置かない（INV-20）
- 受領の内容に壁時計の時刻を入れない。同じ投稿を再照合しても同じ bytes（同じ sha256）になる。
  投稿時刻は PostgreSQL（予約の ``reconciled_at`` / artifact の ``created_at``）が持つ
- Shorts / 長尺を区別する欄を置かない（YouTube API に Shorts の旗は無い。ADR-0020 §9）
- YouTube 固有の上限値はここが唯一の宣言元。adapter も API もここを参照する
"""

from __future__ import annotations

import os
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from contracts.artifact_refs import SHA256_HEX_PATTERN, FrozenModel, SourceArtifactRef
from contracts.render import FINAL_VIDEO_MAX_BYTES
from contracts.states import ArtifactType

UPLOAD_ARTIFACT_SCHEMA_VERSION = "1.0"

#: 冪等キー（upload key）の正準 payload の ``stage``。
UPLOAD_KEY_STAGE = "upload"

# ----------------------------------------------------- YouTube の上限（公式 docs、2026-09-15）

YOUTUBE_TITLE_MAX_CHARS = 100
YOUTUBE_DESCRIPTION_MAX_BYTES = 5000
#: タグの合計長。空白を含むタグは引用符 2 文字、タグ間の区切り 1 文字を数える。
YOUTUBE_TAGS_MAX_TOTAL_CHARS = 500
#: title / description / tags に使えない文字。
YOUTUBE_FORBIDDEN_CHARS = frozenset("<>")
#: resumable upload のチャンクはこの倍数でなければならない（最後のチャンクを除く）。
YOUTUBE_CHUNK_ALIGNMENT_BYTES = 256 * 1024

YOUTUBE_VIDEO_ID_PATTERN = r"^[A-Za-z0-9_-]{11}$"
YOUTUBE_CHANNEL_ID_PATTERN = r"^UC[A-Za-z0-9_-]{22}$"
YOUTUBE_CATEGORY_ID_PATTERN = r"^[0-9]{1,3}$"

# ----------------------------------------------------- 既定値の唯一の宣言元（ADR-0020 §10）

#: チャンク長（8 MiB。``YOUTUBE_CHUNK_ALIGNMENT_BYTES`` の倍数）。
DEFAULT_UPLOAD_CHUNK_BYTES = 8 * 1024 * 1024
#: upload Activity の retry 上限（retryable の型だけ）。
DEFAULT_UPLOAD_MAX_ATTEMPTS = 3
DEFAULT_UPLOAD_HEARTBEAT_TIMEOUT_SECONDS = 60
#: 送信中に heartbeat を送る間隔の上限。
DEFAULT_UPLOAD_HEARTBEAT_INTERVAL_SECONDS = 10
#: 送信結果が読めないとき、uploads playlist でマーカーを探す回数と間隔。
DEFAULT_UPLOAD_MARKER_LOOKUP_ATTEMPTS = 3
DEFAULT_UPLOAD_MARKER_LOOKUP_DELAY_SECONDS = 30
#: upload-media worker の同時投稿数（quota と帯域を占有するので 1）。
DEFAULT_UPLOAD_CONCURRENCY = 1
#: start_to_close の下限と、動画サイズから見積もる最低スループット。
DEFAULT_UPLOAD_MIN_TIMEOUT_SECONDS = 15 * 60
DEFAULT_UPLOAD_MIN_THROUGHPUT_BYTES_PER_SECOND = 256 * 1024
#: 既定のカテゴリ（22 = People & Blogs）。profile / Shorts に依存しない。
DEFAULT_YOUTUBE_CATEGORY_ID = "22"
#: 投稿の公開範囲（INV-19）。処理状態の照合でも同じ値を要求する（ADR-0022）
UPLOAD_PRIVACY_STATUS = "private"
#: 処理状態の照会（ADR-0022）: 初回の待ち・最大間隔・全体の期限（秒）
DEFAULT_UPLOAD_PROCESSING_INITIAL_INTERVAL_SECONDS = 30
DEFAULT_UPLOAD_PROCESSING_MAX_INTERVAL_SECONDS = 10 * 60
DEFAULT_UPLOAD_PROCESSING_DEADLINE_SECONDS = 6 * 60 * 60
UPLOAD_CONTENT_TYPE = "video/mp4"

#: 投稿マーカー（タグ）の接頭辞。uploads playlist の照合で自分の投稿を見分ける。
UPLOAD_MARKER_PREFIX = "avpu"
#: マーカーに使う upload key の hex 桁数（96 bit。1 チャンネル内の衝突は無視できる）。
UPLOAD_MARKER_KEY_CHARS = 24
UPLOAD_MARKER_PATTERN = rf"^{UPLOAD_MARKER_PREFIX}[0-9a-f]{{{UPLOAD_MARKER_KEY_CHARS}}}$"

#: ADR-0070: ``1`` / ``true`` のとき description に投稿マーカーを入れない。
#: 既定 OFF（どの compose / env でも ON にしない）。
#: Workflow 入力には載せない（履歴の形を変えない）。
#: 読み取りはここが唯一の定義元。
UPLOAD_DESCRIPTION_WITHOUT_MARKER_ENV = "AVP_UPLOAD_DESCRIPTION_WITHOUT_MARKER"

#: 視聴者向け description に出してはならない内部 ID（ADR-0070 / INV-90）。
#: builder と insert_body が共有する。
INTERNAL_ID_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(rf"{UPLOAD_MARKER_PREFIX}[0-9a-f]{{{UPLOAD_MARKER_KEY_CHARS}}}", re.IGNORECASE),
    # UUID。``\b`` は CJK・``_`` の直後で効かないので使わない（ハイフンの構造だけで判定）
    re.compile(
        r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}"
        r"-[0-9A-Fa-f]{12}(?![0-9A-Fa-f])"
    ),
    # ハイフン無しの 32 桁以上の hex（UUID / request id / sha256）。直前が "ID" の D のように
    # hex 文字でも拾えるよう、連続長だけで見る（sha1 の 40 桁も止める。誤検出は保留側に倒れる）
    re.compile(r"[0-9A-Fa-f]{32,}"),
    re.compile(r"(?<![A-Za-z0-9])(?:minio|s3)://\S+", re.IGNORECASE),
    # MinIO の object key（domain/artifact/keys.py）: <prefix>/…/<sha256>[.<ext>]
    re.compile(r"(?<![A-Za-z0-9])(?:artifacts|media|research)/[\w./-]*[0-9a-f]{64}(?:\.\w+)?"),
)


def _normalize_for_lint(text: str) -> str:
    """全角を NFKC で畳み、ゼロ幅などの書式文字（Cf）を除いてから検査する。"""
    folded = unicodedata.normalize("NFKC", text)
    return "".join(ch for ch in folded if unicodedata.category(ch) != "Cf")


class MarkerLookupState(StrEnum):
    """マーカー照合（uploads playlist の検索）の結果の3状態（INV-109 / ADR-0075）。

    ``UNKNOWN`` は ``NOT_FOUND`` ではない。API の失敗・timeout・quota・auth・応答の形の崩れ・
    ページの打ち切りは「無い」を意味しないので、新しい ``videos.insert`` の根拠にならない。
    """

    FOUND = "found"
    NOT_FOUND = "not_found"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class MarkerLookup:
    """照合の結果。``video_id`` は FOUND のときだけ、``reason`` は UNKNOWN のときだけ持つ。

    ``reason`` は短い識別子（例外の型名など）。secret・URL・応答本文は入れない。
    """

    state: MarkerLookupState
    video_id: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if (self.state is MarkerLookupState.FOUND) != bool(self.video_id):
            raise ValueError("video_id is required for FOUND and forbidden otherwise")
        if self.state is not MarkerLookupState.UNKNOWN and self.reason is not None:
            raise ValueError("reason is only for UNKNOWN")

    @classmethod
    def found(cls, video_id: str) -> MarkerLookup:
        return cls(MarkerLookupState.FOUND, video_id=video_id)

    @classmethod
    def not_found(cls) -> MarkerLookup:
        return cls(MarkerLookupState.NOT_FOUND)

    @classmethod
    def unknown(cls, reason: str) -> MarkerLookup:
        return cls(MarkerLookupState.UNKNOWN, reason=reason)


UploadReconciledBy = Literal["upload_response", "status_query", "marker_lookup"]


def upload_marker(upload_key: str) -> str:
    """upload key から投稿マーカー（タグ）を導出する。空白・記号を含まない（引用符が付かない）。"""
    if not re.fullmatch(SHA256_HEX_PATTERN, upload_key):
        raise ValueError("upload_key must be a lowercase sha256 hex digest")
    return f"{UPLOAD_MARKER_PREFIX}{upload_key[:UPLOAD_MARKER_KEY_CHARS]}"


def description_without_marker_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """ADR-0070 の flag。既定 OFF（未設定・空・``1`` / ``true`` 以外は OFF）。"""
    env = os.environ if environ is None else environ
    return env.get(UPLOAD_DESCRIPTION_WITHOUT_MARKER_ENV, "").strip().lower() in {"1", "true"}


def marker_in_tags(marker: str, tags: object) -> bool:
    """タグにマーカーがあるか。照合（adapter）と確認ツールが同じ判定を使う（AGENTS.md §8）。"""
    return isinstance(tags, (list, tuple)) and marker in tags


def marker_in_description_lines(marker: str, description: object) -> bool:
    """description のどれかの行（前後の空白を除く）がマーカーそのものか。部分一致は数えない。"""
    return isinstance(description, str) and marker in (
        line.strip() for line in description.splitlines()
    )


class InternalIdInDescriptionError(ValueError):
    """description に内部 ID（marker・UUID・MinIO key）が含まれる（INV-90）。"""


class DescriptionRequiredPartsTooLongError(ValueError):
    """必須部分（クレジット・出典）だけで description の上限を超える（INV-92）。切り詰めない。"""


def find_internal_ids(text: str) -> list[str]:
    """``text`` に含まれる内部 ID らしき文字列（なければ空）。定義は INTERNAL_ID_PATTERNS のみ。"""
    normalized = _normalize_for_lint(text)
    return [m.group(0) for pattern in INTERNAL_ID_PATTERNS for m in pattern.finditer(normalized)]


def assert_no_internal_ids(text: str, field: str = "description") -> None:
    found = find_internal_ids(text)
    if found:
        # 値そのものは例外文に出さない（ログへ内部 ID を運ばない）
        raise InternalIdInDescriptionError(f"{field} contains {len(found)} internal id(s)")


def youtube_tags_length(tags: tuple[str, ...] | list[str]) -> int:
    """YouTube が数えるタグの合計長（空白を含むタグは引用符 2 文字、区切り 1 文字）。"""
    total = sum(len(tag) + (2 if " " in tag else 0) for tag in tags)
    return total + max(len(tags) - 1, 0)


def upload_timeout_seconds(size_bytes: int) -> int:
    """upload Activity の start_to_close を動画サイズから見積もる。"""
    by_size = -(-size_bytes // DEFAULT_UPLOAD_MIN_THROUGHPUT_BYTES_PER_SECOND)
    return max(DEFAULT_UPLOAD_MIN_TIMEOUT_SECONDS, DEFAULT_UPLOAD_MIN_TIMEOUT_SECONDS + by_size)


def _reject_forbidden(value: str, field: str) -> str:
    if YOUTUBE_FORBIDDEN_CHARS & set(value):
        raise ValueError(f"{field} must not contain '<' or '>'")
    return value


def sanitize_youtube_text(value: str) -> str:
    """``<`` ``>`` を全角へ置き換える（決定的。意味を落とさない）。"""
    return value.replace("<", "＜").replace(">", "＞")


def truncate_utf8(value: str, max_bytes: int) -> str:
    """UTF-8 で ``max_bytes`` 以下になるよう文字境界で切る。"""
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


class YouTubeVideoMetadata(FrozenModel):
    """``videos.insert`` に送る snippet / status と通知の設定。送った値の snapshot でもある。"""

    title: str = Field(min_length=1, max_length=YOUTUBE_TITLE_MAX_CHARS)
    description: str
    tags: tuple[str, ...]
    category_id: str = Field(pattern=YOUTUBE_CATEGORY_ID_PATTERN)
    default_language: Literal["ja", "en"] | None = None
    #: INV-19。public / unlisted はスキーマで表現できない。
    privacy_status: Literal["private"]
    self_declared_made_for_kids: bool
    #: AI 生成物なので投稿側は常に True を送る（builder が固定する）。
    contains_synthetic_media: bool
    notify_subscribers: Literal[False]

    @field_validator("title")
    @classmethod
    def _title(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("title must not be blank")
        return _reject_forbidden(value, "title")

    @field_validator("description")
    @classmethod
    def _description(cls, value: str) -> str:
        if len(value.encode("utf-8")) > YOUTUBE_DESCRIPTION_MAX_BYTES:
            raise ValueError(f"description exceeds {YOUTUBE_DESCRIPTION_MAX_BYTES} UTF-8 bytes")
        return _reject_forbidden(value, "description")

    @field_validator("tags")
    @classmethod
    def _tags(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for tag in value:
            if not tag.strip() or "," in tag:
                raise ValueError("tags must be non-blank and must not contain ','")
            _reject_forbidden(tag, "tags")
        if len(set(value)) != len(value):
            raise ValueError("tags must be unique")
        if youtube_tags_length(value) > YOUTUBE_TAGS_MAX_TOTAL_CHARS:
            raise ValueError(f"tags exceed {YOUTUBE_TAGS_MAX_TOTAL_CHARS} characters in total")
        return value


def build_youtube_metadata(
    *,
    upload_key: str,
    title: str,
    hook: str,
    narration: str,
    language: Literal["ja", "en"],
    category_id: str = DEFAULT_YOUTUBE_CATEGORY_ID,
    made_for_kids: bool = False,
    required_parts: Sequence[str] = (),
    marker_in_description: bool | None = None,
) -> YouTubeVideoMetadata:
    """台本の値から決定的にメタデータを組む（ADR-0020 §5 / ADR-0070）。

    同じ入力なら同じメタデータ。タグは投稿マーカーだけ（照合に必須、それ以外は足さない）。

    - ``marker_in_description=None`` なら環境変数 AVP_UPLOAD_DESCRIPTION_WITHOUT_MARKER（既定 OFF）
      で決める。OFF（既定）: 従来どおりマーカーが description の最終行にも入る。``required_parts``
      があればマーカーの前に置く（本文より先に枠を確保する）
    - ON: description にマーカーを入れない。``required_parts``（クレジット・出典）を先に確保して
      残りを本文に割り当て、必須部分だけで 5000 bytes を超えたら切らずに例外（INV-92）。
      組み立てた description は内部 ID の lint を通す（INV-90）
    """
    safe_title = sanitize_youtube_text(title.strip())[:YOUTUBE_TITLE_MAX_CHARS]
    body = sanitize_youtube_text(f"{hook.strip()}\n\n{narration.strip()}".strip())
    marker = upload_marker(upload_key)
    if marker_in_description is None:
        marker_in_description = not description_without_marker_enabled()
    parts = [t for t in (sanitize_youtube_text(p.strip()) for p in required_parts) if t]
    if marker_in_description:
        # マーカーは description の最終行にも置く（タグが編集・除去されても照合できる）
        parts.append(marker)
    required = "\n\n".join(parts)
    required_bytes = len(required.encode("utf-8"))
    if required_bytes > YOUTUBE_DESCRIPTION_MAX_BYTES:
        raise DescriptionRequiredPartsTooLongError(
            f"required description parts need {required_bytes} UTF-8 bytes "
            f"(limit {YOUTUBE_DESCRIPTION_MAX_BYTES})"
        )
    separator = "\n\n" if required else ""
    # 本文だけを切り詰め、必須部分（マーカー・クレジット・出典）は必ず残す
    budget = YOUTUBE_DESCRIPTION_MAX_BYTES - required_bytes - len(separator.encode("utf-8"))
    if budget > 0 and (body or marker_in_description):
        # marker ありは従来と byte 単位で同じ形を保つ。marker 無しは本文が空なら区切りを出さない
        description = truncate_utf8(body, budget) + separator + required
    else:
        description = required
    if not marker_in_description:
        assert_no_internal_ids(description)
    return YouTubeVideoMetadata(
        title=safe_title,
        description=description,
        tags=(marker,),
        category_id=category_id,
        default_language=language,
        privacy_status="private",
        self_declared_made_for_kids=made_for_kids,
        contains_synthetic_media=True,
        notify_subscribers=False,
    )


class UploadDestination(FrozenModel):
    """投稿先。``channel_id`` は upload key の destination に入る。"""

    platform: Literal["youtube"]
    channel_id: str = Field(pattern=YOUTUBE_CHANNEL_ID_PATTERN)


class UploadReceiptArtifact(FrozenModel):
    """投稿受領（ADR-0020）。どの完成動画を・どこへ・どの video id で・何を送ったか。"""

    episode_id: str = Field(min_length=1, max_length=64)
    type: Literal[ArtifactType.UPLOAD_RECEIPT]
    schema_version: Literal["1.0"]
    source_final_video: SourceArtifactRef
    destination: UploadDestination
    video_id: str = Field(pattern=YOUTUBE_VIDEO_ID_PATTERN)
    privacy_status: Literal["private"]
    metadata: YouTubeVideoMetadata
    upload_key: str = Field(pattern=SHA256_HEX_PATTERN)
    bytes: int = Field(gt=0, le=FINAL_VIDEO_MAX_BYTES)
    reconciled_by: UploadReconciledBy

    @model_validator(mode="after")
    def _consistent(self) -> UploadReceiptArtifact:
        if upload_marker(self.upload_key) not in self.metadata.tags:
            raise ValueError("metadata.tags must carry the upload marker of upload_key")
        if self.privacy_status != self.metadata.privacy_status:
            raise ValueError("privacy_status must match metadata.privacy_status")
        return self


def build_upload_receipt(
    *,
    episode_id: str,
    source_final_video: Any,
    destination: Any,
    video_id: str,
    metadata: Any,
    upload_key: str,
    bytes: int,  # noqa: A002 - 契約のフィールド名に合わせる
    reconciled_by: str,
) -> dict[str, Any]:
    """生成側。build の時点で検証を通してから返す。"""
    artifact = UploadReceiptArtifact.model_validate(
        {
            "episode_id": episode_id,
            "type": ArtifactType.UPLOAD_RECEIPT.value,
            "schema_version": UPLOAD_ARTIFACT_SCHEMA_VERSION,
            "source_final_video": source_final_video,
            "destination": destination,
            "video_id": video_id,
            "privacy_status": "private",
            "metadata": metadata,
            "upload_key": upload_key,
            "bytes": bytes,
            "reconciled_by": reconciled_by,
        }
    )
    return artifact.model_dump(mode="json")


def parse_upload_receipt(payload: dict[str, Any]) -> UploadReceiptArtifact:
    """取り込み側。想定外の schema_version は推測せず ValidationError にする。"""
    return UploadReceiptArtifact.model_validate(payload)


__all__ = [
    "DEFAULT_UPLOAD_PROCESSING_DEADLINE_SECONDS",
    "DEFAULT_UPLOAD_PROCESSING_INITIAL_INTERVAL_SECONDS",
    "DEFAULT_UPLOAD_PROCESSING_MAX_INTERVAL_SECONDS",
    "UPLOAD_PRIVACY_STATUS",
    "DEFAULT_UPLOAD_CHUNK_BYTES",
    "DEFAULT_UPLOAD_CONCURRENCY",
    "DEFAULT_UPLOAD_HEARTBEAT_INTERVAL_SECONDS",
    "DEFAULT_UPLOAD_HEARTBEAT_TIMEOUT_SECONDS",
    "DEFAULT_UPLOAD_MARKER_LOOKUP_ATTEMPTS",
    "DEFAULT_UPLOAD_MARKER_LOOKUP_DELAY_SECONDS",
    "DEFAULT_UPLOAD_MAX_ATTEMPTS",
    "DEFAULT_UPLOAD_MIN_THROUGHPUT_BYTES_PER_SECOND",
    "DEFAULT_UPLOAD_MIN_TIMEOUT_SECONDS",
    "DEFAULT_YOUTUBE_CATEGORY_ID",
    "INTERNAL_ID_PATTERNS",
    "UPLOAD_DESCRIPTION_WITHOUT_MARKER_ENV",
    "UPLOAD_ARTIFACT_SCHEMA_VERSION",
    "UPLOAD_CONTENT_TYPE",
    "UPLOAD_KEY_STAGE",
    "UPLOAD_MARKER_KEY_CHARS",
    "UPLOAD_MARKER_PATTERN",
    "UPLOAD_MARKER_PREFIX",
    "YOUTUBE_CATEGORY_ID_PATTERN",
    "YOUTUBE_CHANNEL_ID_PATTERN",
    "YOUTUBE_CHUNK_ALIGNMENT_BYTES",
    "YOUTUBE_DESCRIPTION_MAX_BYTES",
    "YOUTUBE_FORBIDDEN_CHARS",
    "YOUTUBE_TAGS_MAX_TOTAL_CHARS",
    "YOUTUBE_TITLE_MAX_CHARS",
    "YOUTUBE_VIDEO_ID_PATTERN",
    "DescriptionRequiredPartsTooLongError",
    "InternalIdInDescriptionError",
    "MarkerLookup",
    "MarkerLookupState",
    "UploadDestination",
    "UploadReceiptArtifact",
    "UploadReconciledBy",
    "YouTubeVideoMetadata",
    "build_upload_receipt",
    "assert_no_internal_ids",
    "marker_in_description_lines",
    "marker_in_tags",
    "build_youtube_metadata",
    "description_without_marker_enabled",
    "find_internal_ids",
    "parse_upload_receipt",
    "sanitize_youtube_text",
    "truncate_utf8",
    "upload_marker",
    "upload_timeout_seconds",
    "youtube_tags_length",
]
