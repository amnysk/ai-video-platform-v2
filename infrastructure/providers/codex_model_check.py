"""Codex に渡る model の事前検査（起動時）。

実際に使われる model は ``CODEX_MODEL``（``-m``）> ``~/.codex/config.toml`` の順。
許可リストは ``contracts.codex_models`` の ``CODEX_ALLOWED_MODELS`` だけが出所で、
**既定は空（未検証）**。空のときは「使える」と主張せず警告に留める。
"""

from __future__ import annotations

import json
import logging
import os
import tomllib
from datetime import UTC, datetime
from pathlib import Path

from contracts.codex_models import parse_allowlist
from domain.codex_preflight import CatalogSnapshot, CodexModelCheck, evaluate_codex_model
from domain.errors import UnsupportedModelError

logger = logging.getLogger(__name__)


def default_config_toml() -> Path:
    home = os.environ.get("CODEX_HOME") or str(Path(os.environ.get("HOME", "~")) / ".codex")
    return Path(home).expanduser() / "config.toml"


def configured_model_from_toml(path: Path) -> str | None:
    """トップレベルの ``model`` だけを読む。読めなければ ``None``（検査を止めない）。"""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    value = data.get("model")
    return value if isinstance(value, str) and value else None


def effective_model(model_setting: str, config_toml: Path) -> str | None:
    return model_setting or configured_model_from_toml(config_toml)


def verify_codex_model(
    *, model_setting: str, allowed: frozenset[str], config_toml: Path | None = None
) -> None:
    """許可リストがあり、model がその外なら ``UnsupportedModelError``。"""
    model = effective_model(model_setting, config_toml or default_config_toml())
    if model is None:
        logger.warning("codex model: not pinned and not readable from config.toml (unverified)")
        return
    if not allowed:
        logger.warning(
            "codex model %r is unverified: CODEX_ALLOWED_MODELS is empty "
            "(see docs/operations/codex-model-config.md)",
            model,
        )
        return
    if model not in allowed:
        raise UnsupportedModelError(
            f"codex model {model!r} is not in CODEX_ALLOWED_MODELS ({', '.join(sorted(allowed))})",
            model=model,
        )
    logger.info("codex model %r is in the allowlist", model)


def codex_home_dir() -> Path:
    return default_config_toml().parent


def read_models_cache(path: Path) -> CatalogSnapshot | None:
    """``models_cache.json`` の slug 一覧と取得時刻。読めなければ ``None``（検査を止めない）。

    このファイルは codex が認証済みアカウントのために自分で更新するカタログで、読むだけなら
    利用枠を使わない。**載っていても使える保証にはならない**（domain/codex_preflight.py）。
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        slugs = frozenset(
            m["slug"]
            for m in data["models"]
            if isinstance(m, dict) and isinstance(m.get("slug"), str)
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None
    fetched_at: datetime | None = None
    raw = data.get("fetched_at")
    if isinstance(raw, str):
        try:
            # 末尾 Z と 9 桁の小数秒（codex の書式）を fromisoformat が読める形にする
            head, _, frac = raw.rstrip("Z").partition(".")
            text = f"{head}.{frac[:6]}" if frac else head
            fetched_at = datetime.fromisoformat(text)
            if fetched_at.tzinfo is None:
                fetched_at = fetched_at.replace(tzinfo=UTC)
        except ValueError:
            fetched_at = None
    return CatalogSnapshot(slugs=slugs, fetched_at=fetched_at)


def run_codex_preflight(
    *,
    model_setting: str,
    allowed_raw: str,
    require_pinned: bool,
    codex_home: Path | None = None,
    now: datetime | None = None,
) -> CodexModelCheck:
    """設定ファイルだけを読んで判定する。codex を起動せず、prompt を送らない。"""
    home = codex_home or codex_home_dir()
    return evaluate_codex_model(
        model_setting=model_setting,
        config_model=configured_model_from_toml(home / "config.toml"),
        allowed=parse_allowlist(allowed_raw),
        require_pinned=require_pinned,
        catalog=read_models_cache(home / "models_cache.json"),
        now=now or datetime.now(UTC),
    )
