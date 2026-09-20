"""メディア生成器の境界（INV-6: domain は純粋。I/O をしない）。

provider 名・モデル名・provider job id の形式・物理パスをここに置かない（ADR-0017）。

- 画像・動画は**非同期ジョブ型**: ``submit`` → 参照を台帳へ commit → ``poll`` → ``download``。
  ``submit`` は有料で、呼び出し側が予約台帳（ADR-0013）を通してから1回だけ呼ぶ。
  ``poll`` / ``download`` は参照に対して冪等で、再送を伴わない
- 音声は**同期・ローカル・非課金**: ``synthesize`` だけ（台帳の対象外。ADR-0017）

失敗は ``domain.errors`` の型で表す: 拒否は ``ProviderRejectedError``、ジョブ失敗は
``ProviderJobFailedError``、submit の結果不明は ``ProviderSubmitAmbiguousError``。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import NewType, Protocol, runtime_checkable

#: provider が発行したジョブの不透明な参照。中身を解釈しない（形式は adapter だけが知る）。
ProviderJobRef = NewType("ProviderJobRef", str)


@dataclass(frozen=True, slots=True)
class ImageRequest:
    prompt: str
    width: int
    height: int
    #: ``"9:16"`` のようなアスペクト比の表記
    aspect: str


@dataclass(frozen=True, slots=True)
class VideoRequest:
    prompt: str
    #: 元画像のバイト列（adapter が provider へ渡す形に変換する）
    source_image: bytes
    source_image_mime: str
    duration_ms: int
    aspect: str


@dataclass(frozen=True, slots=True)
class JobPending:
    """まだ終わっていない。"""


@dataclass(frozen=True, slots=True)
class JobSucceeded:
    """完了した。本体は ``download`` で取る。"""


@dataclass(frozen=True, slots=True)
class JobFailed:
    """provider 側で失敗した。``rejected`` はポリシー拒否など人間の判断が要る失敗。"""

    message: str
    rejected: bool = False


JobStatus = JobPending | JobSucceeded | JobFailed


@runtime_checkable
class MediaDestination(Protocol):
    """ダウンロード先。物理パスを domain に出さないための書き込み口。"""

    async def write(self, chunk: bytes) -> None: ...


@runtime_checkable
class ImageGenerator(Protocol):
    @property
    def generator_id(self) -> str: ...

    @property
    def generation_profile_id(self) -> str: ...

    def estimate_cost_usd(self, request: ImageRequest) -> float: ...

    async def submit(self, request: ImageRequest) -> ProviderJobRef: ...

    async def poll(self, ref: ProviderJobRef) -> JobStatus: ...

    async def download(self, ref: ProviderJobRef, dest: MediaDestination) -> None: ...


@runtime_checkable
class VideoGenerator(Protocol):
    @property
    def generator_id(self) -> str: ...

    @property
    def generation_profile_id(self) -> str: ...

    def supported_duration_ms(self, requested_ms: int) -> int:
        """provider が実際に作れる尺（例: 5秒刻み）へ丸めた値。"""
        ...

    def estimate_cost_usd(self, request: VideoRequest) -> float: ...

    async def submit(self, request: VideoRequest) -> ProviderJobRef: ...

    async def poll(self, ref: ProviderJobRef) -> JobStatus: ...

    async def download(self, ref: ProviderJobRef, dest: MediaDestination) -> None: ...


@runtime_checkable
class VoiceGenerator(Protocol):
    @property
    def generator_id(self) -> str: ...

    @property
    def generation_profile_id(self) -> str: ...

    @property
    def voice_id(self) -> str: ...

    async def synthesize(self, text: str, language: str, dest: MediaDestination) -> None: ...


@runtime_checkable
class SpeedAdjustableVoiceGenerator(VoiceGenerator, Protocol):
    """話速を指定して合成できる生成器（ADR-0027）。``speed_permille`` は等速 = 1000。

    合成した音声が台本シーンの区間を超えたときだけ、上限つきで話速を上げて合成し直すのに使う。
    持たない生成器は、超過をそのまま ``VoiceExceedsSceneSpanError`` にする。
    """

    async def synthesize_at_speed(
        self, text: str, language: str, dest: MediaDestination, *, speed_permille: int
    ) -> None: ...


__all__ = [
    "ImageGenerator",
    "ImageRequest",
    "JobFailed",
    "JobPending",
    "JobStatus",
    "JobSucceeded",
    "MediaDestination",
    "ProviderJobRef",
    "VideoGenerator",
    "VideoRequest",
    "SpeedAdjustableVoiceGenerator",
    "VoiceGenerator",
]
