"""ログ基盤（deploy/logging）の設定が ADR-0040 §4〜§7 の決定から外れていないこと。

設定ファイルを読むだけで、コンテナは起動しない。
理由は docs/testing/logging-platform-rationale.md §2。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
LOGGING = ROOT / "deploy" / "logging"


def _yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    return _yaml(LOGGING / "compose.logging.yaml")


@pytest.fixture(scope="module")
def fb() -> dict[str, Any]:
    return _yaml(LOGGING / "fluent-bit" / "fluent-bit.yaml")


def _versions_digests() -> dict[str, str]:
    """VERSIONS.md の表: tag 付きの名前 -> index digest。"""
    text = (LOGGING / "VERSIONS.md").read_text(encoding="utf-8")
    rows = re.findall(r"^\| ([^|]+?) \| ([0-9.]+)[^|]*\| `(sha256:[0-9a-f]{64})` \|", text, re.M)
    names = {
        "OpenSearch": "opensearchproject/opensearch",
        "OpenSearch Dashboards": "opensearchproject/opensearch-dashboards",
        "Fluent Bit": "fluent/fluent-bit",
    }
    return {f"{names[n]}:{tag}": digest for n, tag, digest in rows}


# ------------------------------------------------------------------ 版の固定（§7）
def test_images_are_pinned_by_digest_and_match_versions_md(compose) -> None:
    expected = _versions_digests()
    assert len(expected) == 3
    images = {svc["image"] for svc in compose["services"].values()}
    for image in images:
        ref, _, digest = image.partition("@")
        assert digest, f"{image}: digest で固定していない"
        assert expected.get(ref) == digest, f"{image}: VERSIONS.md と食い違う"
    os_tag = next(i for i in images if i.startswith("opensearchproject/opensearch:"))
    osd_tag = next(i for i in images if i.startswith("opensearchproject/opensearch-dashboards:"))
    assert os_tag.split("@")[0].split(":")[1] == osd_tag.split("@")[0].split(":")[1]


def test_scripts_use_the_pinned_opensearch_image(compose) -> None:
    os_image = compose["services"]["opensearch"]["image"]
    for name in ("check-pipeline.sh", "fb-metrics.sh"):
        assert os_image in (LOGGING / "scripts" / name).read_text(encoding="utf-8"), name


# ------------------------------------------------------------------ 公開・権限（§6）
def test_published_ports_are_loopback_only(compose) -> None:
    for name, svc in compose["services"].items():
        for port in svc.get("ports") or []:
            assert str(port).startswith("127.0.0.1:"), f"{name}: {port}"


def test_no_docker_socket_anywhere() -> None:
    for path in LOGGING.rglob("*"):
        if path.is_file() and path.suffix in {".yaml", ".yml", ".sh", ".py"}:
            assert "docker.sock" not in path.read_text(encoding="utf-8"), path


def test_fluent_bit_hardening(compose) -> None:
    svc = compose["services"]["fluent-bit"]
    assert svc["read_only"] is True
    assert svc["cap_drop"] == ["ALL"] and "cap_add" not in svc
    assert "no-new-privileges:true" in svc["security_opt"]
    # uid 1000 では containers/ に入れず無音で何も読めない（実測）
    assert svc["user"] == "0:0"
    assert svc["networks"] == ["internal"], "Fluent Bit に外への経路を持たせない"
    assert compose["networks"]["internal"]["internal"] is True
    assert "ports" not in svc
    # 補間の既定値に ":" を含むので右から割る（source:target:mode）
    mounts = {v.rsplit(":", 2)[1]: v.rsplit(":", 2) for v in svc["volumes"] if isinstance(v, str)}
    source, _, mode = mounts["/containers"]
    assert mode == "ro"
    assert source.endswith("/containers}"), "docker root 全体を渡さない"


def test_long_running_services_drop_capabilities(compose) -> None:
    for name in ("opensearch", "fluent-bit", "dashboards"):
        svc = compose["services"][name]
        assert svc["cap_drop"] == ["ALL"] and "cap_add" not in svc, name
        assert "no-new-privileges:true" in svc["security_opt"], name


def test_secrets_are_not_passed_as_env(compose) -> None:
    for name, svc in compose["services"].items():
        for key in svc.get("environment") or {}:
            assert not re.search(r"PASS|PASSWORD|SECRET|TOKEN", key), f"{name}: {key}"


SECRETS_ROOT = "${AVP_LOGGING_SECRETS_DIR:-${HOME}/.config/avp-logging/${AVP_LOGGING_ENV:-prod}}"

#: サービスごとに mount してよい秘密のファイル（AVP_LOGGING_SECRETS_DIR からの相対）。I-9:
#: ディレクトリごと渡さない（ca.key・node.key・admin.key を必要の無いコンテナに見せない）
ALLOWED_SECRET_FILES: dict[str, set[str]] = {
    "fluent-bit": {"fluent-bit-secret.yaml", "pki/ca.pem"},
    "security-init": {
        "pki/ca.pem",
        "pki/node.pem",
        "pki/node.key",
        "fluentbit.pw",
        "viewer.pw",
        "dashboards.pw",
    },
    "bootstrap": {"pki/ca.pem", "pki/admin.pem", "pki/admin.key"},
    "securityadmin": {"pki/ca.pem", "pki/admin.pem", "pki/admin.key"},
    "dashboards-keystore": {"dashboards.pw", "dashboards-cookie.pw"},
    "dashboards-import": {"viewer.pw"},
}


def _secret_sources(svc: dict[str, Any]) -> set[str]:
    out = set()
    for v in svc.get("volumes") or []:
        source = v["source"] if isinstance(v, dict) else v.rsplit(":", 2)[0]
        if source.startswith(SECRETS_ROOT):
            out.add(source[len(SECRETS_ROOT) :].lstrip("/"))
    return out


def test_secrets_are_mounted_file_by_file(compose) -> None:
    for name, svc in compose["services"].items():
        mounted = _secret_sources(svc)
        assert mounted <= ALLOWED_SECRET_FILES.get(name, set()), f"{name}: {sorted(mounted)}"
        assert "" not in mounted and "pki" not in mounted, f"{name}: ディレクトリごと mount"
        assert "pki/ca.key" not in mounted, f"{name}: CA の秘密鍵はどのコンテナにも渡さない"


def test_admin_certificate_only_in_setup_oneshots(compose) -> None:
    for name, svc in compose["services"].items():
        mounted = _secret_sources(svc)
        if any(m.endswith(".key") or m.endswith(".pw") for m in mounted) and name != "fluent-bit":
            assert set(svc.get("profiles") or []) <= {"setup", "dashboards-setup"}, name
            assert svc.get("profiles"), f"{name}: 秘密を mount する常駐サービス"
        if "pki/admin.key" in mounted:
            assert name in {"bootstrap", "securityadmin"}, name


def test_repo_has_no_secret_material() -> None:
    for path in LOGGING.rglob("*"):
        if not path.is_file():
            continue
        assert path.suffix not in {".key", ".pw", ".p12", ".jks"}, path
        text = path.read_text(encoding="utf-8", errors="ignore")
        assert "PRIVATE KEY-----" not in text, path
        assert not re.search(r"\$2[aby]\$\d\d\$", text), f"{path}: bcrypt hash"


# ------------------------------------------------------------------ 資源（§7）
def test_resource_limits(compose) -> None:
    os_svc = compose["services"]["opensearch"]
    assert os_svc["mem_limit"] == os_svc["memswap_limit"], "swap を使わない"
    assert "-Xms${AVP_LOGGING_OS_HEAP:-1g} -Xmx${AVP_LOGGING_OS_HEAP:-1g}" in str(
        os_svc["environment"]["OPENSEARCH_JAVA_OPTS"]
    )
    assert os_svc["mem_limit"] == "${AVP_LOGGING_OS_MEM_LIMIT:-2560m}"
    assert os_svc["environment"]["DISABLE_INSTALL_DEMO_CONFIG"] == "true"
    assert os_svc["environment"]["DISABLE_PERFORMANCE_ANALYZER_AGENT_CLI"] == "true"
    fb_svc = compose["services"]["fluent-bit"]
    assert fb_svc["mem_limit"] == fb_svc["memswap_limit"] == "256m"
    osd = compose["services"]["dashboards"]
    assert osd["mem_limit"] == osd["memswap_limit"] == "1g"
    assert osd["profiles"] == ["dashboards"], "Dashboards は使う時だけ"
    for name in ("opensearch", "fluent-bit", "dashboards"):
        assert compose["services"][name]["oom_score_adj"] > 0, name


def test_state_volumes_are_guarded(compose) -> None:
    services = compose["services"]
    assert "sentinel" in " ".join(services["opensearch"]["command"])
    deps = services["fluent-bit"]["depends_on"]
    assert deps["fluent-bit-guard"]["condition"] == "service_completed_successfully"
    guard = (LOGGING / "scripts" / "volume-guard.sh").read_text(encoding="utf-8")
    assert ".avp-logging-sentinel" in guard and "tail.db" in guard


# ------------------------------------------------------------------ Fluent Bit（§5）
def _one(items: list[dict[str, Any]], **match: str) -> dict[str, Any]:
    found = [i for i in items if all(i.get(k) == v for k, v in match.items())]
    assert len(found) == 1, match
    return found[0]


def test_tail_input(fb) -> None:
    tail = _one(fb["pipeline"]["inputs"], name="tail")
    assert tail["path"] == "/containers/*/*-json.log*", "rotation 済みも inode で続きから読む"
    assert tail["read_from_head"] == "${AVP_LOG_READ_FROM_HEAD}"
    assert tail["db"].startswith("/fb-state/") and tail["db.locking"] is True
    assert tail["buffer_max_size"] == "256k"
    assert tail["skip_long_lines"] is True
    assert tail["multiline.parser"] == "docker"
    assert tail["storage.type"] == "filesystem"


def test_service_scheduler_and_storage(fb) -> None:
    service = fb["service"]
    assert service["scheduler.base"] == 5 and service["scheduler.cap"] == 300
    assert service["storage.path"] == "${AVP_LOG_STORAGE_PATH}"
    assert service["http_server"] is True and service["health_check"] is True


@pytest.mark.parametrize("alias", ["avp_app", "avp_infra"])
def test_opensearch_outputs(fb, alias) -> None:
    out = _one(fb["pipeline"]["outputs"], alias=alias)
    series = alias.split("_")[1]
    assert out["index"] == f"avp-{series}-${{AVP_LOGGING_ENV}}-write", "書き込み先は固定値"
    assert out["write_operation"] == "create"
    assert out["suppress_type_name"] is True
    assert out["tls"] is True and out["tls.verify"] is True
    assert out["tls.verify_hostname"] is True, "既定 off"
    assert out["buffer_size"] == "4M"
    assert out["retry_limit"] == 72
    assert out["trace_error"] is False and out["trace_output"] is False
    assert out["storage.total_limit_size"] == "${AVP_LOG_STORAGE_LIMIT}"
    assert out["http_passwd"] == "${AVP_OS_WRITER_PASSWORD}"
    if series == "app":
        assert out["id_key"] == "event_id" and "generate_id" not in out
    else:
        assert out["generate_id"] is True


def test_collector_routes_by_exact_project_and_label() -> None:
    lua = (LOGGING / "fluent-bit" / "lua" / "avp_collector.lua").read_text(encoding="utf-8")
    assert "attrs[PROJECT_ATTR] ~= TARGET_PROJECT" in lua, "compose project は完全一致"
    assert "C.app_label" in lua and "C.app_label_value" in lua


# ------------------------------------------------------------------ OpenSearch（§4/§6）
def test_opensearch_yml() -> None:
    conf = _yaml(LOGGING / "opensearch" / "opensearch.yml")
    assert conf["action.auto_create_index"] == "-avp-*,+*"
    assert conf["plugins.security.ssl.http.enabled"] is True
    assert conf["plugins.security.allow_unsafe_democertificates"] is False
    assert conf["bootstrap.memory_lock"] is False
    # DN は RFC2253 順（CN が先）。init-secrets.sh の -subj は OU を先に書く
    for dn in conf["plugins.security.authcz.admin_dn"] + conf["plugins.security.nodes_dn"]:
        assert dn.startswith("CN=") and dn.endswith(",OU=avp2-logging"), dn
    secrets = (LOGGING / "scripts" / "init-secrets.sh").read_text(encoding="utf-8")
    assert '-subj "/OU=avp2-logging/CN=$cn"' in secrets


def test_writer_role_cannot_read_delete_or_create_indices() -> None:
    roles = _yaml(LOGGING / "opensearch" / "security" / "roles.yml")
    writer = roles["avp_log_writer"]
    assert writer["cluster_permissions"] == ["indices:data/write/bulk"]
    (perm,) = writer["index_permissions"]
    assert set(perm["index_patterns"]) == {"avp-app-*", "avp-infra-*"}
    assert set(perm["allowed_actions"]) == {"indices:data/write/index", "indices:data/write/bulk*"}
    viewer = roles["avp_log_viewer"]
    for p in viewer["index_permissions"]:
        assert not any("write" in a or "delete" in a or a == "crud" for a in p["allowed_actions"])


def test_internal_users_are_ours_only() -> None:
    tmpl = _yaml(LOGGING / "opensearch" / "security" / "internal_users.yml.tmpl")
    users = {k for k in tmpl if k != "_meta"}
    assert users == {"avp_fluentbit", "avp_viewer", "avp_dashboards"}, "demo ユーザーを持ち込まない"
    config = _yaml(LOGGING / "opensearch" / "security" / "config.yml")
    dynamic = config["config"]["dynamic"]
    assert dynamic["kibana"]["server_username"] == "avp_dashboards"
    assert dynamic["http"]["anonymous_auth_enabled"] is False
    mapping = _yaml(LOGGING / "opensearch" / "security" / "roles_mapping.yml")
    assert mapping["all_access"]["users"] == []
    assert mapping["kibana_server"]["users"] == ["avp_dashboards"]


@pytest.mark.parametrize(
    ("series", "size", "age", "delete"), [("app", "5gb", "7d", "14d"), ("infra", "1gb", "3d", "7d")]
)
def test_ism_policies(series, size, age, delete) -> None:
    policy = json.loads((LOGGING / f"opensearch/ism/avp-{series}.json").read_text("utf-8"))[
        "policy"
    ]
    hot = next(s for s in policy["states"] if s["name"] == "hot")
    assert hot["actions"] == [{"rollover": {"min_primary_shard_size": size, "min_index_age": age}}]
    assert hot["transitions"] == [
        {"state_name": "delete", "conditions": {"min_rollover_age": delete}}
    ]
    assert policy["ism_template"] == [{"index_patterns": [f"avp-{series}-*"], "priority": 100}]


def test_test_ism_policies_are_only_used_by_the_test_override(compose) -> None:
    assert compose["services"]["bootstrap"]["environment"]["AVP_ISM_POLICY_DIR"] == (
        "${AVP_ISM_POLICY_DIR:-ism}"
    )
    override = _yaml(LOGGING / "compose.test.yaml")
    assert override["services"]["bootstrap"]["environment"]["AVP_ISM_POLICY_DIR"] == "ism-test"


def test_bootstrap_order_and_alias_guard() -> None:
    src = (LOGGING / "scripts" / "bootstrap.py").read_text(encoding="utf-8")
    main = src[src.index("def main()") :]
    order = [
        "check_auto_create()",
        "check_alias_not_an_index(s)",
        "put_pipeline()",
        "put_component(s)",
        "put_index_template(",
        "put_policy(s)",
        "ensure_write_index(s)",
    ]
    positions = [main.index(step) for step in order]
    assert positions == sorted(positions), "ADR-0040 §4 の順序"
    assert '"plugins.index_state_management.rollover_alias"' in src
    assert '"index.default_pipeline": PIPELINE' in src
    assert 'EXPECTED_AUTO_CREATE = "-avp-*,+*"' in src


def test_saved_objects_reference_existing_index_patterns() -> None:
    lines = (LOGGING / "dashboards" / "saved-objects.ndjson").read_text("utf-8").splitlines()
    objects = [json.loads(line) for line in lines]
    patterns = {o["id"] for o in objects if o["type"] == "index-pattern"}
    assert patterns == {"avp-app", "avp-infra"}
    searches = [o for o in objects if o["type"] == "search"]
    assert len(searches) >= 8
    for s in searches:
        refs = {r["id"] for r in s["references"]}
        assert refs <= patterns, s["id"]
        json.loads(s["attributes"]["kibanaSavedObjectMeta"]["searchSourceJSON"])


def test_scripts_do_not_put_passwords_on_the_command_line() -> None:
    """`curl -u user:pass` は ps で見える（I-6）。-K の一時 config で渡す。"""
    for path in (LOGGING / "scripts").glob("*.sh"):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"curl[^\n]*\s-u\s", text), path.name


def test_check_pipeline_checks_each_series_and_the_environment() -> None:
    src = (LOGGING / "scripts" / "check-pipeline.sh").read_text(encoding="utf-8")
    assert 'check_lag app "$MAX_LAG_MIN"' in src and 'check_lag infra "$MAX_LAG_INFRA_MIN"' in src
    assert "must_not" in src and "environment" in src and "now-24h" in src


def _size(value: str) -> int:
    """Fluent Bit の size 表記（``256k`` 等。k/m は 1024 倍）を bytes に。"""
    m = re.fullmatch(r"(\d+)([kKmM]?)", value)
    assert m, value
    return int(m.group(1)) * {"": 1, "k": 1024, "m": 1024 * 1024}[m.group(2).lower()]


def test_collector_caps_joined_lines_at_the_tail_buffer_size(fb) -> None:
    """partial を結合した行には tail の buffer_max_size が効かない（実測）。

    Collector が同じ大きさで切る（I-15）。
    """
    from contracts.log_contract import COLLECTOR_LINE_MAX_BYTES

    tail = _one(fb["pipeline"]["inputs"], name="tail")
    assert _size(tail["buffer_max_size"]) == COLLECTOR_LINE_MAX_BYTES
    lua = (LOGGING / "fluent-bit" / "lua" / "avp_collector.lua").read_text(encoding="utf-8")
    assert "C.collector_line_max_bytes" in lua
    assert 'ERR_LINE_TOO_LONG = "line_too_long"' in lua


def test_sanitize_has_no_backtracking_userinfo_rule() -> None:
    """``%a[%w%+%.%-]*://`` は長い英数字の連なりで O(n²) になり、Collector 全体を止めた（I-15）。"""
    lua = (LOGGING / "fluent-bit" / "lua" / "avp_collector.lua").read_text(encoding="utf-8")
    assert "%a[%w%+%.%-]*://" not in lua
    assert "%[parameters: .-%]" not in lua
