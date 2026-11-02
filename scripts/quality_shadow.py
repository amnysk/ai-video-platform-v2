#!/usr/bin/env python
"""品質ゲートの影の評価: 過去 Episode の書き出しに旧 gate と新 gate を当てる（ADR-0087 §8）。

    python scripts/quality_shadow.py EXPORT_DIR [--now 2026-11-02T00:00:00+00:00]
                                                [--inject-transient N] [--no-assume-usage]

**本番に触れない**。入力は読み取り専用の書き出しディレクトリだけで、新 gate は
**インメモリ sqlite + インメモリのストア**に書き出しを再現して走らせる（PostgreSQL・MinIO・
Temporal・provider・YouTube には接続しない）。書き出しには何も書かない。

書き出しの配置（Episode ごとに 1 ディレクトリ。ディレクトリ名は Episode の UUID）::

    EXPORT_DIR/
      <episode_id>/
        final_video.json         # 保存されている FinalVideoArtifact の本文（そのままの JSON）
        script.json              # final_video.source_script が指す ScriptArtifact の本文
        artifact_metadata.json   # artifact_metadata の行の配列。final_video と script の行が要る::
                                 #   [{"id": "<uuid>", "artifact_type": "final_video",
                                 #     "object_key": "...", "sha256": "<64hex>",
                                 #     "input_hash": "<64hex>", "version": 1,
                                 #     "superseded_at": null, "schema_version": "1.0"}, ...]
                                 #   ``id`` は final_video.source_script.artifact_id と一致すること
        usage.json               # 任意: {"bgm": false, "map": false}。render の使用宣言の写し

    ``usage.json`` が無い Episode は既定で「BGM・地図とも不使用」と宣言したものとして評価する
    （過去の Episode は宣言を持たないため。``--no-assume-usage`` で宣言なしにすると
    ``usage_declaration_missing`` の REVIEW になる）。``bgm`` / ``map`` が true の Episode は、
    証跡と producer が書き出しに無いので ``required_check_missing`` の REVIEW になる（本物の欠落）。

2 つの gate:
  - old: ``assess_for_auto_upload``（既定の provider = 技術 QA + 説明欄の lint。書き出しから入力を
    組み立てる。ENFORCE を最初に設計したときの、記録を持たない gate）
  - new: ``QualityGateService``（検査記録を永続し、一時障害は内部で再試行する）

``--inject-transient N``: 新 gate のストアの読み出しを Episode ごとに最初の N 回だけ一時障害
（ConnectionResetError）で失敗させる。N が再試行の上限（3 回試行 = 失敗は 2 回まで）以内なら
``auto_recovered`` に数えられ、超えれば ``inspection_unavailable``（REVIEW。PASS にはならない）。

標準出力は JSON: ``episodes``（Episode ごとの old / new の判定と理由）と ``summary``
（PASS / WARN / REVIEW / BLOCK / inspection_unavailable の率、投稿可能率、回復数、差分）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from contracts.artifacts import parse_final_video, parse_script_artifact  # noqa: E402
from contracts.quality import (  # noqa: E402
    QUALITY_RULE_VERSION,
    QualityCheck,
    QualityGate,
    QualityReason,
    QualityVerdict,
)
from contracts.quality_records import UsageFeature  # noqa: E402
from contracts.upload import (  # noqa: E402
    DescriptionBlockedError,
    InternalIdInDescriptionError,
    upload_marker,
)
from domain.quality.deterministic import (  # noqa: E402
    QualityInputs,
    assess_for_auto_upload,
    technical_qa_provider,
)
from domain.quality.hold_rate import (  # noqa: E402
    GateDecisionSample,
    cause_of,
    decision_rates,
)
from domain.quality.records import CheckSubject  # noqa: E402
from domain.upload.keys import compute_upload_key  # noqa: E402
from domain.upload.metadata_builder import build_upload_metadata  # noqa: E402
from infrastructure.db.models import ArtifactMetadataRow, Base, EpisodeRow  # noqa: E402
from infrastructure.db.quality_repositories import SqlQualityRecordWriter  # noqa: E402
from infrastructure.quality.service import QualityGateService  # noqa: E402
from infrastructure.storage.memory_store import InMemoryArtifactStore  # noqa: E402

DESTINATION = "UC" + "s" * 22  # 影の評価用の仮の投稿先（実在しない）
_METADATA_TYPES = ("final_video", "script")


class FaultInjectingStore:
    """``get_json`` を、``arm(n)`` した回数だけ一時障害で失敗させる（書き出し先のメモリ上のみ）。"""

    def __init__(self, inner: InMemoryArtifactStore) -> None:
        self._inner = inner
        self._remaining = 0

    def arm(self, failures: int) -> None:
        self._remaining = failures

    async def get_json(self, key: str) -> Any:
        if self._remaining > 0:
            self._remaining -= 1
            raise ConnectionResetError("injected transient fault")
        return await self._inner.get_json(key)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


async def _no_sleep(_: float) -> None:
    return None


@dataclass(frozen=True, slots=True)
class _Loaded:
    episode_id: str
    final_body: dict[str, Any]
    script_body: dict[str, Any]
    rows: list[dict[str, Any]]
    usage: dict[str, bool] | None


def _read_json(path: pathlib.Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_episode(directory: pathlib.Path) -> _Loaded:
    uuid.UUID(directory.name)  # ディレクトリ名は Episode の UUID
    usage_path = directory / "usage.json"
    usage = _read_json(usage_path) if usage_path.is_file() else None
    return _Loaded(
        directory.name,
        _read_json(directory / "final_video.json"),
        _read_json(directory / "script.json"),
        _read_json(directory / "artifact_metadata.json"),
        usage,
    )


def _aware(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value).astimezone(UTC) if value else None


def _current_final(rows: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    for row in rows:
        if row.get("artifact_type") == "final_video" and row.get("superseded_at") is None:
            return row
    return None


def _old_gate(item: _Loaded, current: dict[str, Any], now: datetime) -> GateDecisionSample:
    """旧 gate: 既定の provider（技術 QA + 説明欄）に書き出しから作った入力を渡す。"""
    final = parse_final_video(item.final_body)
    script = parse_script_artifact(item.script_body)
    description: str | None = None
    blocked = False
    upload_key = compute_upload_key(
        episode_id=item.episode_id,
        final_video_sha256=final.media.sha256,
        destination_id=DESTINATION,
    )
    try:
        metadata = build_upload_metadata(upload_key=upload_key, script=script, credits=())
        marker = upload_marker(upload_key)
        description = "\n".join(
            line for line in metadata.description.split("\n") if line.strip() != marker
        )
    except (DescriptionBlockedError, InternalIdInDescriptionError):
        blocked = True
    inputs = QualityInputs(
        item.episode_id,
        current["sha256"],
        current["input_hash"],
        technical_qa=final.technical_qa,
        description=description,
    )

    def description_provider(i: QualityInputs) -> Sequence[QualityCheck]:
        from domain.quality.deterministic import description_lint_provider

        if blocked:
            return (
                QualityCheck(
                    QualityGate.METADATA,
                    QualityVerdict.BLOCK,
                    QualityReason.METADATA_LINT_FAILED,
                    "description lint failed",
                    True,
                ),
            )
        return description_lint_provider(i)

    assessment = assess_for_auto_upload(
        inputs,
        now=now,
        rule_version=QUALITY_RULE_VERSION,
        providers=(technical_qa_provider, description_provider),
    )
    return GateDecisionSample(assessment.decision.allowed, assessment.decision.reason_code)


def _verdict_name(verdict: QualityVerdict | None) -> str | None:
    return verdict.value if verdict is not None else None


async def evaluate_export(
    export_dir: pathlib.Path,
    *,
    now: datetime,
    inject_transient: int = 0,
    assume_usage: bool = True,
) -> dict[str, Any]:
    """書き出し全体を評価する（試験から直接呼ぶ）。"""
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    # 破壊的操作ではない: URL は上で固定したインメモリ sqlite で、既存の DB には繋がらない。
    # ガード（PostgreSQL 専用）は通せないので、sqlite のインメモリであることをここで確かめる
    assert engine.dialect.name == "sqlite" and engine.url.database in (None, "", ":memory:")
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: [t.create(c) for t in Base.metadata.sorted_tables])
    factory = async_sessionmaker(engine, expire_on_commit=False)
    inner = InMemoryArtifactStore()
    store = FaultInjectingStore(inner)
    service = QualityGateService(
        session_factory=factory,
        store=store,  # type: ignore[arg-type]
        destination_id=DESTINATION,
        clock=lambda: now,
        sleep=_no_sleep,
    )
    writer = SqlQualityRecordWriter(factory)
    episodes: list[dict[str, Any]] = []
    old_samples: list[GateDecisionSample] = []
    new_samples: list[GateDecisionSample] = []
    recovered_episodes = 0
    try:
        for directory in sorted(p for p in export_dir.iterdir() if p.is_dir()):
            record: dict[str, Any] = {"episode_id": directory.name}
            try:
                item = _load_episode(directory)
                current = _current_final(item.rows)
                if current is None:
                    raise ValueError("no current final_video row in artifact_metadata.json")
            except Exception as exc:  # noqa: BLE001 - 書き出しの不備はその Episode だけの記録にする
                record["export_error"] = f"{type(exc).__name__}: {exc}"[:300]
                episodes.append(record)
                continue
            try:
                old = _old_gate(item, current, now)
            except Exception:  # noqa: BLE001 - 旧 gate が入力を読めない = 旧 gate は拒否（fail-closed）
                old = GateDecisionSample(False, QualityReason.REPORT_EVALUATION_ERROR)
            async with factory() as session:
                session.add(
                    EpisodeRow(id=uuid.UUID(item.episode_id), topic="shadow", status="render_ready")
                )
                for row in item.rows:
                    if row.get("artifact_type") not in _METADATA_TYPES:
                        continue
                    session.add(
                        ArtifactMetadataRow(
                            id=uuid.UUID(row["id"]),
                            episode_id=uuid.UUID(item.episode_id),
                            artifact_type=row["artifact_type"],
                            schema_version=row.get("schema_version", "1.0"),
                            bucket=row.get("bucket", "shadow"),
                            object_key=row["object_key"],
                            sha256=row["sha256"],
                            input_hash=row["input_hash"],
                            version=int(row.get("version", 1)),
                            superseded_at=_aware(row.get("superseded_at")),
                            size_bytes=row.get("size_bytes"),
                        )
                    )
                await session.commit()
            await inner.put_json(current["object_key"], item.final_body)
            for row in item.rows:
                if row.get("artifact_type") == "script":
                    await inner.put_json(row["object_key"], item.script_body)
            subject = CheckSubject(item.episode_id, current["sha256"], current["input_hash"])
            usage = (
                item.usage
                if item.usage is not None
                else ({"bgm": False, "map": False} if assume_usage else None)
            )
            if usage is not None:
                for feature in UsageFeature:
                    used = bool(usage.get(feature.value, False))
                    await writer.declare_usage(
                        subject,
                        feature,
                        used=used,
                        declared_by="shadow",
                        declared_at=now,
                        evidence_ref="shadow-evidence" if used else None,
                    )
            store.arm(inject_transient)
            outcome = await service.evaluate(item.episode_id, manual=False)
            new = GateDecisionSample(
                outcome.assessment.decision.allowed, outcome.assessment.decision.reason_code
            )
            recovered = outcome.recovered_faults > 0 and not outcome.unavailable
            recovered_episodes += int(recovered)
            old_samples.append(old)
            new_samples.append(new)
            record["old"] = {
                "allowed": old.allowed,
                "reason": old.reason.value,
            }
            record["new"] = {
                "allowed": new.allowed,
                "verdict": _verdict_name(outcome.assessment.verdict),
                "reason": new.reason.value,
                "hold_cause": (cause_of(new).value if cause_of(new) else None),
                "recovered_faults": outcome.recovered_faults,
                "unavailable": list(outcome.unavailable),
                "auto_recovered": recovered,
            }
            episodes.append(record)
    finally:
        await engine.dispose()
    old_rates = decision_rates(old_samples)
    new_rates = decision_rates(new_samples)
    evaluated = [e for e in episodes if "export_error" not in e]
    return {
        "now": now.isoformat(),
        "inject_transient": inject_transient,
        "episodes": episodes,
        "summary": {
            "episodes_total": len(episodes),
            "episodes_evaluated": len(evaluated),
            "export_errors": len(episodes) - len(evaluated),
            "old": old_rates.to_dict(),
            "new": new_rates.to_dict(),
            "inspection_error_rate": new_rates.rate(new_rates.inspection_unavailable),
            "auto_upload_allowed_rate": {
                "old": old_rates.rate(old_rates.upload_allowed),
                "new": new_rates.rate(new_rates.upload_allowed),
            },
            "auto_recovered": recovered_episodes,
            # 新 gate だけが許可した / 新 gate だけが拒否した Episode（安全の確認に使う）
            "newly_allowed": [
                e["episode_id"]
                for e in evaluated
                if e["new"]["allowed"] and not e["old"]["allowed"]
            ],
            "newly_refused": [
                e["episode_id"]
                for e in evaluated
                if e["old"]["allowed"] and not e["new"]["allowed"]
            ],
        },
    }


async def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("export_dir", type=pathlib.Path)
    parser.add_argument("--now", type=datetime.fromisoformat, default=None)
    parser.add_argument("--inject-transient", type=int, default=0)
    parser.add_argument("--no-assume-usage", action="store_true")
    args = parser.parse_args(argv)
    if not args.export_dir.is_dir():
        parser.error(f"not a directory: {args.export_dir}")
    now = args.now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    report = await evaluate_export(
        args.export_dir,
        now=now,
        inject_transient=max(0, args.inject_transient),
        assume_usage=not args.no_assume_usage,
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
