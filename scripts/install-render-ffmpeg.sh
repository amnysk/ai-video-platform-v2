#!/usr/bin/env bash
# render worker 用の固定版 static ffmpeg / ffprobe を用意する（Phase 5 / ADR-0019）。冪等。
#
# digest 固定の docker image からバイナリだけを取り出し、sha256 を照合する。
# 照合に失敗したら置かない（途中のファイルは消す）。
#
#   ./scripts/install-render-ffmpeg.sh
#   → RENDER_FFMPEG_PATH / RENDER_FFMPEG_SHA256 等に設定する値を表示する
set -euo pipefail

FFMPEG_VERSION="7.1.1"
IMAGE="mwader/static-ffmpeg:${FFMPEG_VERSION}@sha256:11a44711684c0b9f754c047dcd64235b8b52deab251bd0e0a86f22faa160749c"
FFMPEG_SHA256="810f94020e76e2b58fb44759a322e86bea5d213ebededad7471f3a15b0bf2c5c"
FFPROBE_SHA256="4818b8964b5d7b699370628a4154c97e88205678ee506ca72e9330600e917667"

TOOLS_DIR="${AVP_RENDER_TOOLS_DIR:-$HOME/.local/share/avp/ffmpeg/$FFMPEG_VERSION}"
mkdir -p "$TOOLS_DIR"

sha_ok() {  # $1=path $2=expected
  [ -f "$1" ] && [ "$(sha256sum "$1" | cut -d' ' -f1)" = "$2" ]
}

if ! sha_ok "$TOOLS_DIR/ffmpeg" "$FFMPEG_SHA256" || ! sha_ok "$TOOLS_DIR/ffprobe" "$FFPROBE_SHA256"; then
  staging="$(mktemp -d "$TOOLS_DIR/.staging.XXXXXX")"
  container=""
  cleanup() {
    [ -n "$container" ] && docker rm "$container" >/dev/null 2>&1 || true
    rm -rf "$staging"
  }
  trap cleanup EXIT
  container="$(docker create "$IMAGE")"
  docker cp "$container:/ffmpeg" "$staging/ffmpeg"
  docker cp "$container:/ffprobe" "$staging/ffprobe"
  for pair in "ffmpeg:$FFMPEG_SHA256" "ffprobe:$FFPROBE_SHA256"; do
    name="${pair%%:*}"
    expected="${pair#*:}"
    if ! sha_ok "$staging/$name" "$expected"; then
      echo "sha256 mismatch for $name (expected $expected)" >&2
      exit 1
    fi
    chmod 0755 "$staging/$name"
    mv -f "$staging/$name" "$TOOLS_DIR/$name"
  done
fi

# grep -q が先に閉じると pipefail で SIGPIPE を失敗と誤認するので、一度変数に受ける
filters="$("$TOOLS_DIR/ffmpeg" -hide_banner -filters 2>/dev/null)"
if ! grep -qE '^ *[.TSC|]+ +subtitles +' <<<"$filters"; then
  echo "ffmpeg lacks the subtitles (libass) filter" >&2
  exit 1
fi

"$TOOLS_DIR/ffmpeg" -hide_banner -version | head -n 1
echo
echo "RENDER_FFMPEG_PATH=$TOOLS_DIR/ffmpeg"
echo "RENDER_FFMPEG_SHA256=$FFMPEG_SHA256"
echo "RENDER_FFPROBE_PATH=$TOOLS_DIR/ffprobe"
echo "RENDER_FFPROBE_SHA256=$FFPROBE_SHA256"
