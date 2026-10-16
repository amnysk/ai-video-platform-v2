"""音声の混合規則（ADR-0019 §4）。純粋な記述だけで、実行は描画エンジンの adapter が行う。

- 無音の土台（総尺）に各音声を profile のサンプルレート・ch へ揃えて開始時刻に置く
- 利得は固定 1.0（``gain_permille=1000``。float は input_hash を揺らすので int）
- 正規化・ラウドネス補正はしない（決定性）
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from contracts.render import RenderProfile

#: 混合規則の版。規則を変えたら上げる（input_hash に入る）。
AUDIO_MIX_VERSION = 1


@dataclass(frozen=True, slots=True)
class AudioMixSpec:
    sample_rate_hz: int
    channels: int
    gain_permille: int = 1000
    normalize: bool = False
    loudness_normalization: bool = False
    version: int = AUDIO_MIX_VERSION

    def as_payload(self) -> dict[str, object]:
        return asdict(self)


def audio_mix_spec(profile: RenderProfile) -> AudioMixSpec:
    return AudioMixSpec(
        sample_rate_hz=profile.audio.sample_rate_hz, channels=profile.audio.channels
    )


__all__ = ["AUDIO_MIX_VERSION", "AudioMixSpec", "audio_mix_spec"]
