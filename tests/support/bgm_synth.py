"""検証用の BGM を**コードで合成する**（第三者の音源を使わない。ADR-0083 §1）。

アンビエントのパッド（Am - F - C - G）+ 柔らかいキック + シェイカー。ステレオ・WAV（16bit）。
外部ライブラリ無し（標準ライブラリのみ）。同じ引数なら同じバイト列（乱数は seed 固定）。

シームレスなループ: 曲の長さ ``seconds`` は和音 4 つの周期で、パッドの包絡線も打楽器の
尾も**周期境界で巻き戻して足す**ので、終端と先頭が連続する（ループ継ぎ目のクリックが出ない）。
これは「実在する楽曲」ではなく、結線とミックスの検証のためのテスト素材。権利は自作（self-generated）。
"""

from __future__ import annotations

import io
import math
import random
import wave

SAMPLE_RATE_HZ = 24_000
#: Am - F - C - G（各コードの構成音、Hz）
CHORDS: tuple[tuple[float, ...], ...] = (
    (110.00, 164.81, 220.00, 261.63),  # Am: A2 E3 A3 C4
    (87.31, 174.61, 220.00, 261.63),  # F: F2 F3 A3 C4
    (130.81, 196.00, 261.63, 329.63),  # C: C3 G3 C4 E4
    (98.00, 196.00, 246.94, 293.66),  # G: G2 G3 B3 D4
)
_TWO_PI = 2.0 * math.pi


def _raised_cosine(phase: float) -> float:
    """phase 0..1 → 0..1 の滑らかな立ち上がり。"""
    return 0.5 - 0.5 * math.cos(math.pi * min(max(phase, 0.0), 1.0))


def synthesize(seconds: int = 64, bpm: int = 80, *, rate: int = SAMPLE_RATE_HZ) -> bytes:
    """ステレオ 16bit の WAV を返す。``seconds`` は 4 の倍数（和音 4 つで 1 周期）。"""
    if seconds <= 0 or seconds % 4:
        raise ValueError("seconds must be a positive multiple of 4")
    n = seconds * rate
    seg = n // 4
    fade = seg // 2  # コードの切替で隣り合う 2 つが重なる長さ
    left = [0.0] * n
    right = [0.0] * n

    # --- パッド: コードごとに「周期的な」包絡線（前のコードから次のコードへ滑らかに受け渡す）
    for ci, chord in enumerate(CHORDS):
        start = ci * seg
        voices = []
        for vi, freq in enumerate(chord):
            for detune, side in ((0.997, 0), (1.003, 1)):
                voices.append((freq * detune, 0.55 + 0.45 * (vi % 2), side, 0.37 * (vi + 1) + side))
        for i in range(seg + fade):
            # 自分の区間 [0, seg) で 1、前後 fade で 0 へ（周期境界は index を mod n で巻き戻す）
            if i < fade:
                env = _raised_cosine(i / fade)
            elif i < seg:
                env = 1.0
            else:
                env = 1.0 - _raised_cosine((i - seg) / fade)
            t = i / rate
            idx = (start - fade // 2 + i) % n  # 区間の前半 fade/2 は前のコードに食い込ませる
            trem = 0.85 + 0.15 * math.sin(_TWO_PI * 0.15 * (idx / rate))
            sl = sr = 0.0
            for freq, amp, side, ph in voices:
                value = amp * math.sin(_TWO_PI * freq * t + ph)
                if side:
                    sr += value
                else:
                    sl += value
            g = env * trem * 0.045
            left[idx] += sl * g
            right[idx] += sr * g

    # --- 打楽器: 柔らかいキック（1・3 拍）とシェイカー（8 分）。尾は周期で巻き戻す
    beat = 60.0 / bpm
    rng = random.Random(20260101)
    n_beats = int(seconds / beat)
    kick_len = int(0.30 * rate)
    for b in range(n_beats):
        if b % 2 == 0:
            base = int(b * beat * rate)
            for i in range(kick_len):
                t = i / rate
                freq = 45.0 + 55.0 * math.exp(-t * 18.0)
                v = math.sin(_TWO_PI * freq * t) * math.exp(-t * 11.0) * 0.12
                idx = (base + i) % n
                left[idx] += v
                right[idx] += v
        for half in (0, 1):
            base = int((b + 0.5 * half) * beat * rate)
            last = 0.0
            shake_len = int(0.06 * rate)
            for i in range(shake_len):
                raw = rng.uniform(-1.0, 1.0)
                hp = raw - last  # 簡易ハイパス
                last = raw
                v = hp * math.exp(-i / (0.012 * rate)) * (0.035 if half == 0 else 0.02)
                idx = (base + i) % n
                left[idx] += v * 0.9
                right[idx] += v * 1.1

    peak = max(max(map(abs, left)), max(map(abs, right)), 1e-9)
    scale = 0.9 / peak  # 録音の天井を 0.9 に揃える（ラウドネスは登録時に実測して記録する）
    frames = bytearray()
    for l_, r_ in zip(left, right, strict=True):
        frames += int(l_ * scale * 32767).to_bytes(2, "little", signed=True)
        frames += int(r_ * scale * 32767).to_bytes(2, "little", signed=True)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(bytes(frames))
    return buf.getvalue()


__all__ = ["CHORDS", "SAMPLE_RATE_HZ", "synthesize"]
