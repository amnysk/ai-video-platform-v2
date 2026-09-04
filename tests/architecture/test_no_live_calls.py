"""INV-18: テストとCIから有料API・実投稿へ到達しうる依存を持ち込まない。"""

from __future__ import annotations

import pathlib

REPO = pathlib.Path(__file__).resolve().parents[2]

# Phase 1 では有料provider/YouTubeの実装自体を持たない。
FORBIDDEN_TOKENS = {
    "fal.run": "fal.ai endpoint",
    "fal.ai": "fal.ai",
    "queue.fal": "fal.ai queue",
    "googleapis.com/youtube": "YouTube API",
    "youtube.googleapis.com": "YouTube API",
    "api.openai.com": "OpenAI",
    "api.anthropic.com": "Anthropic",
}

SEARCHED_DIRS = ["apps", "workers", "domain", "infrastructure", "contracts", "tests"]
SELF = pathlib.Path(__file__).resolve()


def test_no_live_provider_endpoints_in_the_codebase() -> None:
    violations: list[str] = []
    for directory in SEARCHED_DIRS:
        for path in sorted((REPO / directory).rglob("*.py")):
            if path.resolve() == SELF:
                continue
            source = path.read_text(encoding="utf-8")
            for token, label in FORBIDDEN_TOKENS.items():
                if token in source:
                    violations.append(f"{path.relative_to(REPO)}: mentions {label} (INV-18)")
    assert not violations, "\n".join(violations)
