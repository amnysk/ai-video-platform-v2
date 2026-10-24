#!/usr/bin/env python
"""Codex の model 設定を、prompt を送らずに診断する（ADR-0058 / INV-75）。

    python scripts/codex-model-preflight.py [--json]

読むのは ``CODEX_MODEL`` / ``CODEX_ALLOWED_MODELS`` / ``CODEX_REQUIRE_PINNED_MODEL``（環境変数）、
``$CODEX_HOME/config.toml``、``$CODEX_HOME/models_cache.json`` だけ。codex を起動せず、
ネットワークにも出ず、ChatGPT の利用枠を使わない。

終了コード: 0 = 止める理由なし（``unverified`` を含む。使える証明ではない） /
2 = 必ず失敗する構成（catalog に無い・許可リストに無い・固定が必須なのに未固定）。
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from infrastructure.providers.codex_model_check import run_codex_preflight  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    check = run_codex_preflight(
        model_setting=os.environ.get("CODEX_MODEL", ""),
        allowed_raw=os.environ.get("CODEX_ALLOWED_MODELS", ""),
        require_pinned=os.environ.get("CODEX_REQUIRE_PINNED_MODEL", "").lower()
        in ("1", "true", "yes"),
    )
    if args.json:
        print(
            json.dumps(
                {
                    "verdict": check.verdict.value,
                    "blocking": check.blocking,
                    "model": check.model,
                    "source": check.source,
                    "message": check.message,
                },
                ensure_ascii=False,
            )
        )
    else:
        print(f"{check.verdict.value}: {check.message}")
    return 2 if check.blocking else 0


if __name__ == "__main__":
    raise SystemExit(main())
