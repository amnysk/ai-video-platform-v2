"""Design-only Agent Studio preset contract tests (no external calls)."""

from __future__ import annotations

import json
from pathlib import Path

from contracts.agents import resolve_agent_spec
from contracts.workflow_templates import TEMPLATES

ROOT = Path(__file__).resolve().parents[2]
PRESET = ROOT / "config" / "presets" / "japans-past-agent-studio-v1.json"


def _preset() -> dict:
    return json.loads(PRESET.read_text(encoding="utf-8"))


def test_inactive_preset_does_not_activate_production() -> None:
    preset = _preset()
    assert preset["status"] == "proposed"
    assert preset["schedule"]["enabled"] is False
    assert preset["agent_template"]["enabled"] is False
    assert preset["youtube"]["privacy_status"] == "private"
    assert preset["youtube"]["allow_automatic_publication"] is False
    assert preset["model_registry"]["live_provider_registration"] is False
    assert preset["safety"]["disable_paid_live_tests"] is True
    assert preset["safety"]["paid_evaluation_requires_human"] is True


def test_agent_roles_specs_and_model_policies_are_registered() -> None:
    from contracts.agents import MODEL_POLICIES

    preset = _preset()
    agents = preset["agents"]
    assert len(agents) == len({a["role"] for a in agents})
    for a in agents:
        role, version = a["spec_ref"].split("@", maxsplit=1)
        assert role == a["role"]
        spec = resolve_agent_spec(role, version)
        assert a["model_policy_ref"] in MODEL_POLICIES
        assert int(a["max_llm_calls"]) <= spec.max_llm_calls
        assert float(a["max_cost_usd"]) <= float(spec.max_estimated_cost_usd)
        assert len(a["editable_prompt"]) >= 40


def test_template_exists_and_all_roles_present() -> None:
    p = _preset()
    t = (p["agent_template"]["id"], p["agent_template"]["version"])
    assert t in TEMPLATES
    assert TEMPLATES[t].runnable is True
    configured = {a["role"] for a in p["agents"]}
    declared = {
        stage.agent_ref.split("@", maxsplit=1)[0]
        for stage in (*TEMPLATES[t].stages, *TEMPLATES[t].post_publish)
        if stage.agent_ref is not None
    }
    assert configured == declared


def test_budget_and_sharing_safe_defaults() -> None:
    p = _preset()
    assert 0 < p["budget"]["channel_monthly_fal_cap_jpy"] <= p["budget"]["global_fal_monthly_cap_jpy"]
    assert p["budget"]["block_if_estimate_unavailable"] is True
    assert p["schedule"]["daily_limit"] == 1
    assert p["schedule"]["overlap"] == "skip"
    assert p["insights"]["share_mode"] == "explicit_approval_only"
    assert p["insights"]["share_raw_analytics"] is False
    assert p["insights"]["share_oauth_or_prompts"] is False
