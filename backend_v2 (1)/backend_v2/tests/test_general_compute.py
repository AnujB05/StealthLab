"""
Tests for General Compute panel wiring (hosted, open-weight, OpenAI-compatible).

Family derivation is the part actually worth testing: it's new logic, and
getting it wrong either falsely rejects a genuinely heterogeneous panel
(annoying) or falsely accepts a homogeneous one (defeats the point of the
heterogeneity check entirely, silently).
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from app.debate.panel import (
    _derive_family,
    assert_heterogeneous,
    general_compute_judge,
    general_compute_panel,
)


# --- Family derivation ---

@pytest.mark.parametrize("model,expected", [
    ("deepseek-v4-pro", "deepseek"),
    ("deepseek/deepseek-v4-flash", "deepseek"),
    ("qwen3.6-235b", "qwen3.6"),
    ("meta/llama-4-scout", "llama"),
    ("mistralai/mistral-large-3", "mistral-large"),
    ("glm-5.2", "glm"),
    ("kimi-k2.7-code", "kimi"),
    ("gpt-oss-120b", "gpt-oss"),
])
def test_family_derivation_strips_version_and_size_tokens(model, expected):
    assert _derive_family(model) == expected


def test_family_derivation_strips_vendor_path_prefix():
    assert _derive_family("moonshotai/kimi-k2") == _derive_family("kimi-k2")


def test_family_derivation_strips_instruct_chat_suffixes():
    assert _derive_family("qwen3-32b-instruct") == _derive_family("qwen3-32b")
    assert _derive_family("llama-4-scout-chat") == _derive_family("llama-4-scout")


def test_deepseek_variants_collapse_to_one_family():
    """
    The actual purpose of this function: two different DeepSeek releases
    should be recognised as sharing lineage, not treated as heterogeneous
    just because their version numbers differ.
    """
    a = _derive_family("deepseek-v4-pro-max")
    b = _derive_family("deepseek-v4-flash")
    assert a == b


def test_genuinely_different_families_stay_different():
    families = {
        _derive_family("deepseek-v4-pro"),
        _derive_family("qwen3.6-235b"),
        _derive_family("glm-5.2"),
        _derive_family("kimi-k2.7-code"),
    }
    assert len(families) == 4


# --- Panel construction and the heterogeneity guarantee ---

def _settings(panel_models="deepseek-v4-pro,qwen3.6-235b,glm-5.2", judge_model="kimi-k2.7-code"):
    s = type("S", (), {})()
    s.general_compute_panel_models = panel_models
    s.general_compute_judge_model = judge_model
    s.general_compute_base_url = "https://api.generalcompute.com"
    return s


def test_panel_requires_at_least_three_models():
    with patch("app.debate.panel.settings", _settings(panel_models="deepseek-v4-pro,qwen3.6-235b")):
        with pytest.raises(ValueError, match="at least 3 models"):
            general_compute_panel()


def test_panel_from_config_passes_heterogeneity_check():
    with patch("app.debate.panel.settings", _settings()):
        panel = general_compute_panel()
    assert len(panel) == 3
    assert_heterogeneous(panel)  # must not raise


def test_panel_correctly_rejects_a_same_family_misconfiguration():
    """
    The scenario the check exists to catch: three models that look
    different by name but share lineage. This must fail loudly at
    construction, not silently run a homogeneous 'panel'.
    """
    with patch(
        "app.debate.panel.settings",
        _settings(panel_models="deepseek-v4-pro,deepseek-v4-flash,deepseek-v3.2"),
    ):
        panel = general_compute_panel()
    with pytest.raises(ValueError, match="not heterogeneous"):
        assert_heterogeneous(panel)


def test_judge_requires_a_model_configured():
    with patch("app.debate.panel.settings", _settings(judge_model="")):
        with pytest.raises(ValueError, match="not set"):
            general_compute_judge()


def test_judge_uses_general_compute_credentials_and_url():
    with patch("app.debate.panel.settings", _settings()):
        judge = general_compute_judge()
    assert judge.api_key_field == "general_compute_api_key"
    assert judge.base_url == "https://api.generalcompute.com"


def test_panel_and_judge_together_are_four_genuinely_distinct_families():
    """
    End to end: the actual four-seat configuration a real test run would
    use, checked as a whole rather than each factory in isolation.
    """
    with patch("app.debate.panel.settings", _settings()):
        panel = general_compute_panel()
        judge = general_compute_judge()

    from app.eval.layer1 import enforce_independence

    assert_heterogeneous(panel)
    enforce_independence(judge, panel)  # must not raise


# --- Regression: chat previously bypassed provider selection entirely ---

def test_chat_agent_respects_general_compute_when_configured():
    """
    chat.py hardcoded AnthropicAgent directly until this was found --
    built before provider selection existed, never updated when it was
    added. Same class of gap Layer 2's wiring had earlier in this project.
    """
    from app.debate.panel import default_chat_agent

    s = _settings()
    s.use_local_models = False
    s.use_general_compute = True
    with patch("app.debate.panel.settings", s):
        agent = default_chat_agent()

    assert agent.model_id == "kimi-k2.7-code"
    assert agent.api_key_field == "general_compute_api_key"
    assert agent.agent_id == "chat"


def test_chat_agent_falls_back_to_anthropic_when_nothing_configured():
    from app.debate.panel import default_chat_agent

    with patch("app.debate.panel.settings") as s:
        s.use_local_models = False
        s.use_general_compute = False
        agent = default_chat_agent()

    assert agent.family == "anthropic"
    assert agent.agent_id == "chat"
