"""Antigravity model catalog and routing aliases."""

from agent.gemini_cloudcode_adapter import _resolve_antigravity_model_id
from hermes_cli.models import _PROVIDER_MODELS


def test_antigravity_catalog_exposes_user_visible_tiers():
    models = _PROVIDER_MODELS["google-antigravity"]

    assert "gemini-3.5-flash-low" in models
    assert "gemini-3.5-flash-medium" in models
    assert "gemini-3.5-flash-high" in models
    assert "gemini-3.1-pro-low" in models
    assert "gemini-3.1-pro-high" in models
    assert "gemini-pro-agent" in models

    # Raw upstream ids should not be shown as first-class picker choices when a
    # clean Antigravity tier name exists.
    assert "gemini-3-flash-agent" not in models


def test_antigravity_flash_tiers_route_to_upstream_ids():
    assert _resolve_antigravity_model_id("gemini-3.5-flash-low") == "gemini-3.5-flash-extra-low"
    assert _resolve_antigravity_model_id("gemini-3.5-flash-medium") == "gemini-3.5-flash-low"
    assert _resolve_antigravity_model_id("gemini-3.5-flash-high") == "gemini-3-flash-agent"


def test_antigravity_pro_tiers_pass_through():
    # Current agy captures show Pro low/high are accepted as suffixed ids.
    assert _resolve_antigravity_model_id("gemini-3.1-pro-low") == "gemini-3.1-pro-low"
    assert _resolve_antigravity_model_id("gemini-3.1-pro-high") == "gemini-3.1-pro-high"
    assert _resolve_antigravity_model_id("gemini-pro-agent") == "gemini-pro-agent"
