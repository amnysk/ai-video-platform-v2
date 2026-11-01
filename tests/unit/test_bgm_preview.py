"""scripts/bgm_preview.py（BGM 試聴ツール）: 引数・遮断・試聴専用の印・本番の拒否を緩めないこと。

理由は docs/testing/bgm-preview-rationale.md。ffmpeg が要るテストは固定版が無ければ skip。
"""

from __future__ import annotations

import ast
import importlib.util
import json
import socket
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from domain.music.placement import PlacementRefusal, SceneRole, plan_cue_sheet
from domain.render.bgm import BgmSkipReason, resolve_bgm
from tests.support.bgm_catalog import NOW, make_track
from tests.support.bgm_render import bgm_plan, cue, sheet

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "bgm_preview.py"
FFMPEG = Path.home() / ".local/share/avp/ffmpeg/7.1.1/ffmpeg"
needs_ffmpeg = pytest.mark.skipif(not FFMPEG.is_file(), reason="pinned ffmpeg not installed")


def _load():
    spec = importlib.util.spec_from_file_location("bgm_preview_cli", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["bgm_preview_cli"] = module
    spec.loader.exec_module(module)
    return module


bp = _load()


# ---------------------------------------------------------------- 引数
def test_offsets_parse_dedupe_and_reject_garbage() -> None:
    assert bp.parse_offsets("-3,0, 3,0") == (-3, 0, 3)
    with pytest.raises(bp.PreviewError):
        bp.parse_offsets("a,b")
    with pytest.raises(bp.PreviewError):
        bp.parse_offsets(" , ")


def test_roles_parse_and_reject_unknown_role() -> None:
    assert bp.parse_roles("s1=intro, s3=silent") == {
        "s1": SceneRole.INTRO,
        "s3": SceneRole.SILENT,
    }
    assert bp.parse_roles(None) == {}
    with pytest.raises(bp.PreviewError):
        bp.parse_roles("s1=loud")


def test_render_requires_a_track_and_out() -> None:
    parser = bp.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["render", "exp", "--catalog", "c", "--out", "o"])
    args = parser.parse_args(
        ["render", "exp", "--catalog", "c", "--track", "a", "--track", "a", "--out", "o"]
    )
    opts = bp.options_from_args(args)
    assert opts.track_ids == ("a",) and opts.offsets == (0,) and not opts.allow_unapproved


def test_main_returns_2_for_input_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = bp.main(
        ["render", str(tmp_path), "--catalog", str(tmp_path), "--track", "x", "--out",
         str(tmp_path / "o"), "--ffmpeg", str(tmp_path / "nope")]
    )  # fmt: skip
    assert code == 2
    assert "bgm-preview:" in capsys.readouterr().err


def test_output_dir_may_not_overlap_the_export_or_minio_data(tmp_path: Path) -> None:
    export = tmp_path / "exp"
    export.mkdir()
    bp.check_out_dir(tmp_path / "out", export)
    for bad in (export, export / "sub", tmp_path, bp.MINIO_DATA_DIR / "x"):
        with pytest.raises(bp.PreviewError):
            bp.check_out_dir(bad, export)


# ---------------------------------------------------------------- 通信・fal
def test_offline_guard_blocks_tcp_and_dns_and_restores() -> None:
    real = socket.socket.connect
    with bp.offline_guard() as state:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        with pytest.raises(bp.PreviewError):
            s.connect(("203.0.113.1", 9))
        s.close()
        with pytest.raises(bp.PreviewError):
            socket.getaddrinfo("example.invalid", 80)
        assert len(state.blocked_connects) == 2
    assert socket.socket.connect is real


def test_paid_path_modules_loaded_is_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in [m for m in sys.modules if m.startswith(bp.FORBIDDEN_MODULE_PREFIXES)]:
        monkeypatch.delitem(sys.modules, name)  # 同じプロセスの他のテストが読んでいても独立に
    bp.assert_no_paid_path_loaded()
    monkeypatch.setitem(sys.modules, "infrastructure.providers.fal_queue", object())
    with pytest.raises(bp.PreviewError, match="fal_queue"):
        bp.assert_no_paid_path_loaded()


def test_importing_the_tool_does_not_load_fal_or_production_workers() -> None:
    code = (
        "import importlib.util,sys;"
        f"s=importlib.util.spec_from_file_location('m',r'{SCRIPT}');"
        "m=importlib.util.module_from_spec(s);sys.modules['m']=m;s.loader.exec_module(m);"
        "import workers.render.activities;"
        "print(m.loaded_forbidden_modules())"
    )
    done = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT,
        env={"PYTHONPATH": str(ROOT), "PATH": "/usr/bin:/bin"}, check=True,
    )  # fmt: skip
    assert done.stdout.strip() == "[]"


# ---------------------------------------------------------------- 本番は緩めない / 試聴だけ通す
def _unapproved():
    return make_track(
        "bgm-unapproved",
        "b",
        license_status="unverified",
        license_evidence_sha256=None,
        license_checked_at=None,
    )  # noqa: E501


def test_production_path_still_rejects_an_unapproved_track() -> None:
    plan = bgm_plan()
    track = _unapproved()
    assert plan_cue_sheet(plan, track).refusal is PlacementRefusal.TRACK_NOT_APPROVED
    resolved = resolve_bgm(
        sheet(cue(track_id="bgm-unapproved", source_sha256="b" * 64)), [track],
        plan=plan, now=NOW, file_sha256_by_track={"bgm-unapproved": "b" * 64},
    )  # fmt: skip
    assert not resolved.applied
    assert resolved.rejections[0].reason is BgmSkipReason.LICENSE_REJECTED
    assert "license_not_approved" in resolved.rejections[0].detail


def test_preview_override_makes_only_the_copy_pass_and_lists_changes() -> None:
    plan = bgm_plan()
    original = _unapproved()
    copy, changed = bp.preview_override(original, now=NOW, profile_format="shorts")
    assert original.license_status == "unverified"  # 元は変えない
    assert copy.license_status == "approved"
    assert {"license_status", "license_evidence_sha256", "license_checked_at"} <= set(changed)
    resolved = resolve_bgm(
        sheet(
            cue(
                track_id="bgm-unapproved",
                source_sha256="b" * 64,
                license_evidence_hash=copy.license_evidence_sha256,
            )
        ),
        [copy],
        plan=plan, now=NOW, file_sha256_by_track={"bgm-unapproved": "b" * 64},
    )  # fmt: skip
    assert resolved.applied


def test_preview_override_leaves_an_approved_track_alone() -> None:
    track = make_track()
    same, changed = bp.preview_override(track, now=NOW, profile_format="shorts")
    assert same is track and changed == ()


def test_shift_levels_moves_and_clamps_cue_levels() -> None:
    s = sheet(cue(target_relative_level_db=-18))
    assert bp.shift_levels(s, 6).cues[0].target_relative_level_db == -12
    assert bp.shift_levels(s, 99).cues[0].target_relative_level_db == 0
    assert bp.shift_levels(s, -999).cues[0].target_relative_level_db == -96


# ------------------------------------------------------------ 試聴専用の印・カタログの複製
def test_every_output_name_and_the_mark_say_preview_only() -> None:
    assert bp.PREVIEW_MARK == "PREVIEW ONLY - NOT FOR UPLOAD"
    assert bp.FILE_PREFIX == "PREVIEW_ONLY_NOT_FOR_UPLOAD"


@needs_ffmpeg
def test_unapproved_track_needs_the_flag_and_the_source_catalog_is_untouched(
    tmp_path: Path,
) -> None:
    cat = tmp_path / "cat"
    tid = bp.make_synthetic_catalog(cat, seconds=4)
    before = (cat / "catalog.json").read_bytes()
    with pytest.raises(bp.PreviewError, match="--allow-unapproved"):
        bp.prepare_catalog(
            cat, [tid], tmp_path / "s1", allow_unapproved=False, ffmpeg=FFMPEG, now=NOW
        )
    prepared = bp.prepare_catalog(
        cat, [tid], tmp_path / "s2", allow_unapproved=True, ffmpeg=FFMPEG, now=NOW
    )
    p = prepared[tid]
    assert p.original_status == "unverified" and p.track.license_status == "approved"
    assert "license_not_approved" in p.production_reject_reasons
    assert p.loudness_measured and p.track.loudness_lufs is not None
    scratch_catalog = json.loads((tmp_path / "s2" / "catalog.json").read_text())
    assert scratch_catalog["tracks"][0]["license_status"] == "approved"
    assert (cat / "catalog.json").read_bytes() == before  # 元のカタログは読むだけ


@needs_ffmpeg
def test_tampered_audio_and_unknown_track_are_refused(tmp_path: Path) -> None:
    cat = tmp_path / "cat"
    tid = bp.make_synthetic_catalog(cat, seconds=4)
    with pytest.raises(bp.PreviewError, match="not in catalog"):
        bp.prepare_catalog(
            cat, ["nope"], tmp_path / "s", allow_unapproved=True, ffmpeg=FFMPEG, now=NOW
        )
    audio = next((cat / "music" / "originals").iterdir())
    audio.write_bytes(audio.read_bytes() + b"x")
    with pytest.raises(bp.PreviewError, match="sha256"):
        bp.prepare_catalog(
            cat, [tid], tmp_path / "s", allow_unapproved=True, ffmpeg=FFMPEG, now=NOW
        )


@needs_ffmpeg
def test_watermark_and_phone_simulation_on_a_tiny_clip(tmp_path: Path) -> None:
    src = tmp_path / "in.mp4"
    subprocess.run(
        [str(FFMPEG), "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=black:s=64x64:d=1",
         "-f", "lavfi", "-i", "sine=f=1000:d=1", "-shortest", "-c:v", "mpeg4",
         "-c:a", "aac", str(src)],
        check=True,
    )  # fmt: skip
    out, wav = tmp_path / "out.mp4", tmp_path / "phone.wav"
    bp.watermark_remux(FFMPEG, src, out, "note")
    bp.phone_simulation(FFMPEG, out, wav)
    probe = subprocess.run(
        [str(FFMPEG.with_name("ffprobe")), "-v", "error", "-show_format", "-of", "json", str(out)],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    tags = json.loads(probe.stdout)["format"]["tags"]
    assert tags["title"] == bp.PREVIEW_MARK and bp.PREVIEW_MARK in tags["comment"]
    assert wav.is_file() and wav.stat().st_size > 1000


def test_sync_check_flags_drift() -> None:
    ok = {"format_duration_s": 4.0, "video": {"duration_s": 4.0, "start_time_s": 0},
          "audio": {"duration_s": 4.0, "start_time_s": 0}}  # fmt: skip
    assert bp.sync_check(ok, 4000)["ok"]
    bad = {**ok, "audio": {"duration_s": 4.2, "start_time_s": 0}}
    assert not bp.sync_check(bad, 4000)["ok"]


# ---------------------------------------------------------------- export は読み取り専用
def test_export_function_has_no_write_calls() -> None:
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "export_episode"
    )  # noqa: E501
    called = {
        n.func.attr
        for n in ast.walk(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }  # noqa: E501
    forbidden = {"put_json", "put_bytes", "put_text", "put_file", "remove", "remove_object",
                 "delete", "add", "commit", "flush", "merge", "update", "insert"}  # fmt: skip
    assert not (called & forbidden), called & forbidden


async def test_export_reads_current_artifacts_and_never_writes(tmp_path: Path) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import StaticPool

    from infrastructure.db.models import ArtifactMetadataRow, Base, EpisodeRow
    from infrastructure.storage.memory_store import InMemoryArtifactStore

    class ReadOnlyStore(InMemoryArtifactStore):
        async def put_json(self, *a, **k):  # type: ignore[no-untyped-def]
            raise AssertionError("write to store")

        put_bytes = put_text = put_file = put_json

    engine = create_async_engine(
        "sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    store = ReadOnlyStore()
    ep = uuid.uuid4()
    await InMemoryArtifactStore.put_json(store, "k/script.json", {"schema_version": "1.0", "a": 1})
    async with factory() as session:
        session.add(EpisodeRow(id=ep, status="assets_ready", topic="t"))
        await session.flush()
        session.add(
            ArtifactMetadataRow(
                id=uuid.uuid4(),
                episode_id=ep,
                artifact_type="script",
                schema_version="1.0",
                bucket="artifacts",
                object_key="k/script.json",
                sha256="a" * 64,
                input_hash="h",
                version=1,
                size_bytes=1,
                scene_id=None,
            )  # fmt: skip
        )
        await session.commit()
    result = await bp.export_episode(factory, store, str(ep), tmp_path / "exp")
    assert result["artifacts"] == {"script": 1}
    assert json.loads((tmp_path / "exp" / "script" / f"{'a' * 64}.json").read_text())["a"] == 1


def test_now_is_timezone_aware_constant() -> None:
    assert NOW.tzinfo is UTC and isinstance(datetime.now(UTC), datetime)
