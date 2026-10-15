"""``scripts/intervene.py propose-storyboard``（ADR-0043 §1）。理由は jpmas2-p0b-rationale.md。"""

from __future__ import annotations

import importlib.util
import io
import json
import pathlib
from typing import Any

REPO = pathlib.Path(__file__).resolve().parents[2]
EP = "11111111-2222-3333-4444-555555555555"
TOKEN = "cli-secret-token-value-42"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("intervene_cli_sb", REPO / "scripts/intervene.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cli = _load()


def _run(argv: list[str], tmp_path: pathlib.Path) -> tuple[int, list[dict[str, Any]], str]:
    proposal = tmp_path / "proposal.json"
    proposal.write_text(
        json.dumps(
            {
                "episode_id": EP,
                "base_storyboard_revision_id": "rev-1",
                "edits": [{"scene_id": "sb1", "visual_description": "castle"}],
            }
        )
    )
    sent: list[dict[str, Any]] = []

    def transport(method: str, url: str, headers: dict[str, str], data: bytes | None):
        sent.append({"method": method, "url": url, "json": json.loads(data or b"null")})
        return 202, b'{"commands": []}'

    err = io.StringIO()
    code = cli.main(
        [*argv, "--file", str(proposal)],
        env={"AVP_INTERVENTION_TOKEN": TOKEN},
        transport=transport,
        stdout=io.StringIO(),
        stderr=err,
    )
    return code, sent, err.getvalue()


def test_it_posts_the_proposal_to_the_application_route(tmp_path: pathlib.Path) -> None:
    code, sent, err = _run(
        ["propose-storyboard", "--episode", EP, "--command-id", "req-00000001",
         "--expected-control-revision", "4"],
        tmp_path,
    )  # fmt: skip
    assert code == 0
    [request] = sent
    assert request["method"] == "POST"
    assert request["url"].endswith(f"/v1/intervention/episodes/{EP}/storyboard/proposals")
    assert request["json"]["command_id"] == "req-00000001"
    assert request["json"]["expected_control_revision"] == 4
    assert request["json"]["proposal"]["edits"][0]["scene_id"] == "sb1"
    assert "command_id=req-00000001" in err  # 再送のために stderr に出す


def test_without_a_command_id_it_makes_one_and_omits_unset_options(tmp_path: pathlib.Path) -> None:
    code, sent, _err = _run(["propose-storyboard", "--episode", EP], tmp_path)
    assert code == 0
    body = sent[0]["json"]
    assert len(body["command_id"]) >= 8 and "expected_control_revision" not in body
