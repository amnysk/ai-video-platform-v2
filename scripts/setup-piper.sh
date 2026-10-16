#!/usr/bin/env bash
# Piper（ローカル TTS）を**隔離 venv**に用意する（ADR-0017 §5）。冪等。
#
# piper-tts は GPL-3.0。プラットフォームのコードからは import しない。
# worker は infrastructure/providers/piper_cli/synthesize.py をこの venv の python で
# 子プロセスとして起動する。共有 .venv には入れないこと。
#
#   ./scripts/setup-piper.sh
#   → PIPER_PYTHON / PIPER_VOICE_PATH に設定する値を表示する
set -euo pipefail

PIPER_HOME="${PIPER_HOME:-$HOME/.local/share/avp2/piper}"
PIPER_TTS_VERSION="1.8.0"
VOICE="en_US-kristin-medium"
# 公開ドメイン（LibriVox）で scratch 学習された声。lessac 系・NC ライセンスの声は使わない。
VOICE_BASE_URL="https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/kristin/medium"

VENV="$PIPER_HOME/venv"
VOICES="$PIPER_HOME/voices"
mkdir -p "$VOICES"

PYTHON_BIN="${PYTHON_BIN:-python3}"
if [ ! -x "$VENV/bin/python" ]; then
  "$PYTHON_BIN" -m venv "$VENV"
fi
installed="$("$VENV/bin/python" -c 'import importlib.metadata as m; print(m.version("piper-tts"))' 2>/dev/null || true)"
if [ "$installed" != "$PIPER_TTS_VERSION" ]; then
  "$VENV/bin/pip" install --quiet --disable-pip-version-check "piper-tts==$PIPER_TTS_VERSION"
fi

for suffix in onnx onnx.json; do
  target="$VOICES/$VOICE.$suffix"
  if [ ! -s "$target" ]; then
    curl -fsSL --retry 3 -o "$target.part" "$VOICE_BASE_URL/$VOICE.$suffix"
    mv "$target.part" "$target"
  fi
done

echo "piper-tts : $("$VENV/bin/python" -c 'import importlib.metadata as m; print(m.version("piper-tts"))')"
sha256sum "$VOICES/$VOICE.onnx" "$VOICES/$VOICE.onnx.json"
echo
echo "PIPER_PYTHON=$VENV/bin/python"
echo "PIPER_VOICE_PATH=$VOICES/$VOICE.onnx"
