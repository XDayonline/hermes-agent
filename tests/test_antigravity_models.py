"""Antigravity model catalog and routing aliases."""

from agent.antigravity_code_assist import parse_agent_model_ids
from agent.gemini_cloudcode_adapter import _resolve_antigravity_model_id
from hermes_cli.models import _PROVIDER_MODELS


def test_antigravity_catalog_exposes_user_visible_tiers():
    models = _PROVIDER_MODELS["google-antigravity"]

    # Real upstream IDs as exposed by the Antigravity API (no renaming)
    assert "gemini-3.5-flash-extra-low" in models   # Flash Low
    assert "gemini-3.5-flash-low" in models          # Flash Medium
    assert "gemini-3-flash-agent" in models           # Flash High
    assert "gemini-3.1-pro-low" in models             # Pro Low
    assert "gemini-pro-agent" in models               # Pro High

    # Synthetic renamed IDs we previously invented must NOT appear
    assert "gemini-3.5-flash-medium" not in models
    assert "gemini-3.5-flash-high" not in models
    assert "gemini-3.1-pro-high" not in models


def test_antigravity_flash_tiers_route_to_upstream_ids():
    assert _resolve_antigravity_model_id("gemini-3.5-flash-low") == "gemini-3.5-flash-extra-low"
    assert _resolve_antigravity_model_id("gemini-3.5-flash-medium") == "gemini-3.5-flash-low"
    assert _resolve_antigravity_model_id("gemini-3.5-flash-high") == "gemini-3-flash-agent"


def test_antigravity_pro_tiers_pass_through():
    # Keep the picker clean and translate the high tier to the Antigravity
    # upstream wire id only at dispatch time.
    assert _resolve_antigravity_model_id("gemini-3.1-pro-low") == "gemini-3.1-pro-low"
    assert _resolve_antigravity_model_id("gemini-3.1-pro-high") == "gemini-pro-agent"
    assert _resolve_antigravity_model_id("gemini-pro-agent") == "gemini-pro-agent"


def test_antigravity_live_agent_model_groups_are_parsed_and_canonicalized():
    payload = {
        "agentModelSorts": [
            {
                "displayName": "Recommended",
                "groups": [
                    {
                        "modelIds": [
                            "gemini-3.5-flash-low",
                            "gemini-3-flash-agent",
                            "gemini-3.5-flash-extra-low",
                            "gemini-3.1-pro-low",
                            "gemini-pro-agent",
                            "chat_23310",
                            "tab_flash_lite_preview",
                        ]
                    }
                ],
            }
        ]
    }

    # parse_agent_model_ids returns the real upstream IDs directly (no renaming)
    assert parse_agent_model_ids(payload) == [
        "gemini-3.5-flash-low",
        "gemini-3-flash-agent",
        "gemini-3.5-flash-extra-low",
        "gemini-3.1-pro-low",
        "gemini-pro-agent",
    ]
