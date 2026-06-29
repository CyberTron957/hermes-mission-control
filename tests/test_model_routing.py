"""Phase 1 — route-aware config + pricing.

The teams pins context window / auxiliary model / vision and prices tokens itself
ONLY for a custom / OpenAI-compatible endpoint (any base_url), which hides the
real model from Hermes. For a NATIVE provider it must DEFER to Hermes so its
always-current model metadata + pricing apply. These tests pin that split.

A custom endpoint is configured the normal way — `hermes setup`'s custom
provider, or the dashboard — which surfaces here as an explicit base_url. There
is no teams-specific proxy env var.
"""

import tempfile
from pathlib import Path

import yaml

from teams_server.config import write_agent_hermes_config
from teams_server.model_config import estimate_cost_usd


def _write(**kw):
    d = Path(tempfile.mkdtemp())
    write_agent_hermes_config(d, **kw)
    return yaml.safe_load((d / "config.yaml").read_text()), d


# --------------------------------------------------------------------------- #
# config.yaml gating
# --------------------------------------------------------------------------- #
def test_native_route_defers_window_aux_vision():
    cfg, _ = _write(model="claude-opus-4-8", provider="anthropic",
                    base_url="", api_key="sk-ant")
    m = cfg["model"]
    aux = cfg.get("auxiliary", {})
    # No window pin → Hermes resolves the real per-model window.
    assert "context_length" not in m
    # Native provider → no base_url written.
    assert "base_url" not in m
    assert m["provider"] == "anthropic"
    # No managed aux pins → Hermes picks a cheap default_aux_model + native vision.
    for task in ("compression", "title_generation", "vision", "web_extract"):
        assert task not in aux
    # Compression itself stays enabled (a teams tuning choice, route-independent).
    assert cfg["compression"]["enabled"] is True


def test_custom_endpoint_route_pins_window_aux_vision():
    cfg, _ = _write(model="my-custom-model", provider="custom",
                    base_url="https://api.example.com/v1", api_key="sk-test")
    m = cfg["model"]
    aux = cfg["auxiliary"]
    assert m["context_length"] == 256000
    assert m["base_url"] == "https://api.example.com/v1"
    # Aux tasks pinned to our endpoint so provider 'auto' can't escape to an
    # unauthenticated aggregator mid-compaction.
    assert aux["compression"]["base_url"] == "https://api.example.com/v1"
    assert aux["compression"]["context_length"] == 256000
    assert "vision" in aux


def test_custom_to_native_switch_strips_stale_pins():
    # Re-writing a custom-endpoint home as native must remove the old pins, else
    # the agent keeps dialing the dead endpoint for aux/vision.
    d = Path(tempfile.mkdtemp())
    write_agent_hermes_config(d, model="my-custom-model", provider="custom",
                              base_url="https://api.example.com/v1", api_key="sk-test")
    write_agent_hermes_config(d, model="claude-opus-4-8", provider="anthropic",
                              base_url="", api_key="sk-ant")
    cfg = yaml.safe_load((d / "config.yaml").read_text())
    m = cfg["model"]
    aux = cfg.get("auxiliary", {})
    assert "context_length" not in m and "base_url" not in m
    for task in ("compression", "title_generation", "vision", "web_extract"):
        assert task not in aux


# --------------------------------------------------------------------------- #
# pricing
# --------------------------------------------------------------------------- #
def test_custom_endpoint_model_priced_from_table():
    # A model served behind a custom endpoint and listed in MODEL_PRICES:
    # 1M in + 1M out at (0.19, 0.51) = 0.70.
    assert estimate_cost_usd("kimi", 1_000_000, 1_000_000, 0) == 0.7


def test_native_model_priced_by_hermes_when_provider_given():
    cost = estimate_cost_usd("claude-opus-4-8", 1_000_000, 1_000_000, 0,
                             provider="anthropic")
    assert cost is not None and cost > 0


def test_native_model_without_provider_is_na():
    # Legacy DB rows without provider can't resolve a route → n/a, not a guess.
    assert estimate_cost_usd("claude-opus-4-8", 1_000_000, 1_000_000, 0) is None


def test_custom_provider_not_priced_by_hermes():
    # provider 'custom' is opaque to Hermes; not in the table → n/a.
    assert estimate_cost_usd("some-alias", 1_000_000, 1_000_000, 0,
                             provider="custom") is None
