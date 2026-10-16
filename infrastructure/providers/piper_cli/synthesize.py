"""Piper 合成の単独スクリプト（ADR-0017 / Phase 4B）。

**プラットフォームのコードはこのファイルを import しない。** piper-tts は GPL-3.0 なので、
``scripts/setup-piper.sh`` が作る隔離 venv の python で子プロセスとして起動する。
このスクリプトはプラットフォームのパッケージ（contracts / domain 等）を import しない。

入出力:

- ``--version``: ``{"piper_tts": "<version>"}`` を stdout に出して終わる
- 既定: stdin の JSON ``{"model_path", "config_path", "text", "output_path",
  "length_scale", "noise_scale", "noise_w_scale"}`` を読み、``output_path`` へ WAV を書く
  （``.part`` に書いてから rename する）

終了コード（このスクリプト自身の規約。adapter が分類に使う）:

- 0 成功
- 2 入力 JSON が不正
- 3 piper を import できない / 音声モデルを読めない（環境の問題。人間が直す）
- 1 合成中の失敗
"""

from __future__ import annotations

import importlib
import importlib.metadata
import json
import os
import sys
import wave

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_BAD_INPUT = 2
EXIT_UNAVAILABLE = 3

_REQUIRED = ("model_path", "config_path", "text", "output_path")


def _fail(code: int, message: str) -> int:
    sys.stderr.write(message[:4000] + "\n")
    return code


def main(argv: list[str]) -> int:
    if argv[1:] == ["--version"]:
        try:
            version = importlib.metadata.version("piper-tts")
        except importlib.metadata.PackageNotFoundError:
            return _fail(EXIT_UNAVAILABLE, "piper-tts is not installed in this interpreter")
        sys.stdout.write(json.dumps({"piper_tts": version}) + "\n")
        return EXIT_OK

    try:
        request = json.loads(sys.stdin.read())
        if not isinstance(request, dict) or any(
            not isinstance(request.get(k), str) or not request[k] for k in _REQUIRED
        ):
            raise ValueError(f"required string fields: {_REQUIRED}")
    except ValueError as exc:
        return _fail(EXIT_BAD_INPUT, f"invalid request: {exc}")

    try:
        piper = importlib.import_module("piper")
        voice = piper.PiperVoice.load(request["model_path"], config_path=request["config_path"])
    except Exception as exc:  # noqa: BLE001 - 環境の問題はまとめて unavailable
        return _fail(EXIT_UNAVAILABLE, f"cannot load piper voice: {type(exc).__name__}: {exc}")

    output = request["output_path"]
    part = output + ".part"
    try:
        config = piper.SynthesisConfig(
            length_scale=request.get("length_scale"),
            noise_scale=request.get("noise_scale"),
            noise_w_scale=request.get("noise_w_scale"),
        )
        with wave.open(part, "wb") as wav_file:
            voice.synthesize_wav(request["text"], wav_file, syn_config=config)
        os.replace(part, output)
    except Exception as exc:  # noqa: BLE001
        return _fail(EXIT_FAILED, f"synthesis failed: {type(exc).__name__}: {exc}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv))
