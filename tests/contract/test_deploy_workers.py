"""デプロイ手順が全アプリサービスを同じ版で作り直す設計であることの検査（ADR-0024 追補）。

2026-09-20 の調査で、稼働中の worker が3つの git commit（5a1a0cd / f38835c / e29c7e8）の
コードで混在して動いていたことが分かった。共通イメージは1枚なのに、サービスごとの
``build`` / ``up -d <service>`` を繰り返したため、作り直されなかったコンテナが古い image id のまま
残った。ここで固定するのは「手順が1つで、全サービスを列挙し、版が label に残る」こと。
根拠と各テストの意図は docs/testing/worker-versions.md。
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
REVISION_LABEL = "org.opencontainers.image.revision"
SCRIPT = ROOT / "scripts" / "deploy-workers.sh"


def _compose_services() -> dict[str, dict]:
    data = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    return data["services"]


def _built_services() -> dict[str, dict]:
    return {name: svc for name, svc in _compose_services().items() if "build" in svc}


def _bash_array(name: str) -> list[str]:
    text = SCRIPT.read_text(encoding="utf-8")
    match = re.search(rf"^{name}=\(([^)]*)\)", text, re.M)
    assert match, f"{SCRIPT.name} に配列 {name} が無い"
    return match.group(1).split()


def test_app_services_cover_every_service_built_from_this_repo() -> None:
    built = set(_built_services())
    assert len(built) >= 11
    assert set(_bash_array("APP_SERVICES")) == built


def test_services_sharing_an_image_tag_share_one_build_spec() -> None:
    """同じタグを名乗るサービスの build が食い違うと、後からビルドした版がタグを奪う。"""
    specs: dict[str, set[str]] = {}
    for svc in _built_services().values():
        specs.setdefault(svc["image"], set()).add(repr(sorted(svc["build"].items())))
    assert {tag: len(s) for tag, s in specs.items()} == {tag: 1 for tag in specs}


def test_build_services_build_each_image_tag_exactly_once() -> None:
    built = _built_services()
    tags = [built[name]["image"] for name in _bash_array("BUILD_SERVICES")]
    assert sorted(tags) == sorted({svc["image"] for svc in built.values()})


def test_deploy_never_builds_or_recreates_infrastructure() -> None:
    """postgres の bind mount は相対パス。別 worktree から up すると再作成される。"""
    text = SCRIPT.read_text(encoding="utf-8")
    assert re.search(r"up -d --no-deps --no-build", text)
    assert not set(_bash_array("APP_SERVICES")) & {"postgres", "temporal", "minio"}


def test_makefile_deploys_only_through_the_script() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert re.search(r"^deploy-workers:.*\n\t.*scripts/deploy-workers\.sh", makefile, re.M)
    assert re.search(r"^workers-versions:.*\n\t.*scripts/workers-versions\.sh", makefile, re.M)


def test_makefile_wraps_the_deploy_in_a_maintenance_pause() -> None:
    """deploy 中の pause を「解除まで1つの処理」にする（ADR-0027）。

    ``deploy-workers`` が素の ``deploy-workers.sh`` を直接呼ぶと、pause しても
    解除を保証する trap が無い。wrapper 経由であることと、guard に使う python を
    上書きできること（host に venv があるとは限らない）を固定する。
    """
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    recipe = re.search(r"^deploy-workers:.*\n((?:\t.*\n)+)", makefile, re.M)
    assert recipe, "deploy-workers target not found"
    body = recipe.group(1)
    assert "scripts/with-maintenance-pause.sh" in body
    assert "--reason deploy-workers" in body
    assert re.search(r"--\s+\./scripts/deploy-workers\.sh", body)
    assert re.search(r"^PYTHON \?=", makefile, re.M)


def _stage(dockerfile: str, name: str) -> str:
    """``FROM ... AS <name>`` から次の ``FROM`` の手前まで。"""
    body = re.split(rf"^FROM\s+\S+\s+AS\s+{name}\s*$", dockerfile, flags=re.M | re.I)[1]
    return re.split(r"^FROM\s", body, flags=re.M)[0]


def test_app_and_worker_images_carry_revision_label() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    for stage in ("app", "worker"):
        body = _stage(dockerfile, stage)
        assert re.search(r"^ARG GIT_REVISION", body, re.M), stage
        assert re.search(
            rf'^LABEL {re.escape(REVISION_LABEL)}="\$\{{GIT_REVISION\}}"', body, re.M
        ), stage
    assert 'ENV AVP_GIT_REVISION="${GIT_REVISION}"' in _stage(dockerfile, "worker")


def test_base_stage_does_not_depend_on_the_revision() -> None:
    """base の設定（ARG / LABEL / ENV）が revision で変わると、worker stage の重い層が
    revision ごとに再ビルドされる（実測: 約 1 分）。revision は最終 stage にだけ置く。"""
    base = _stage((ROOT / "Dockerfile").read_text(encoding="utf-8"), "base")
    assert "GIT_REVISION" not in base and "AVP_GIT_REVISION" not in base


def test_revision_is_declared_after_the_heavy_layers_of_the_worker_stage() -> None:
    worker = _stage((ROOT / "Dockerfile").read_text(encoding="utf-8"), "worker")
    arg = worker.index("ARG GIT_REVISION")
    for heavy in ("apt-get install", "npm install -g", "piper-tts=="):
        assert arg > worker.index(heavy), heavy


def test_compose_builds_api_migrate_dummy_from_the_app_stage() -> None:
    """base のままだと revision label が付かず、workers-versions.sh が NO-REVISION で落ちる。"""
    for name, svc in _built_services().items():
        expected = "app" if svc["image"] == "avp2-app:local" else "worker"
        assert svc["build"]["target"] == expected, name


def test_versions_script_reads_revision_label() -> None:
    script = (ROOT / "scripts" / "workers-versions.sh").read_text(encoding="utf-8")
    assert REVISION_LABEL in script
