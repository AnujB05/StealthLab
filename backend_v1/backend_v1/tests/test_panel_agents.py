"""
Offline tests for AnthropicAgent/OpenAICompatAgent (MVP plan, Section 7).

What these can and cannot verify: they mock the SDK client objects, so
they confirm this code correctly extracts text from a well-formed SDK
response and correctly raises when a required key is missing. They
prove nothing about whether real API calls succeed, what real models
actually output, or account/network/auth behavior -- that requires
live credentials this environment doesn't have. Genuinely closeable
offline is "does our wrapper code handle the SDK's response shape
correctly"; genuinely not closeable offline is "do real models produce
output our JSON extraction can parse." The latter stays an open risk
until first real usage.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.debate.panel import AnthropicAgent, OpenAICompatAgent, _extract_json


def test_anthropic_agent_extracts_text_from_sdk_response():
    """Mocks anthropic.AsyncAnthropic at the point AnthropicAgent imports it."""
    fake_block = SimpleNamespace(type="text", text="hello from claude")
    fake_response = SimpleNamespace(content=[fake_block])

    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=fake_response)

    with patch("anthropic.AsyncAnthropic", return_value=mock_client):
        agent = AnthropicAgent(agent_id="a")
        with patch("app.debate.panel.settings") as mock_settings:
            mock_settings.require.return_value = "fake-key"
            mock_settings.anthropic_model = "claude-sonnet-4-6"
            result = asyncio.run(agent.respond("system", "user"))

    assert result == "hello from claude"
    mock_client.messages.create.assert_called_once()


def test_anthropic_agent_ignores_non_text_blocks():
    """A real response can mix text and tool_use blocks; only text should concatenate."""
    blocks = [
        SimpleNamespace(type="tool_use", text=None),
        SimpleNamespace(type="text", text="part one "),
        SimpleNamespace(type="text", text="part two"),
    ]
    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=SimpleNamespace(content=blocks))

    with patch("anthropic.AsyncAnthropic", return_value=mock_client):
        agent = AnthropicAgent(agent_id="a")
        with patch("app.debate.panel.settings") as mock_settings:
            mock_settings.require.return_value = "fake-key"
            mock_settings.anthropic_model = "claude-sonnet-4-6"
            result = asyncio.run(agent.respond("system", "user"))

    assert result == "part one part two"


def test_openai_compat_agent_extracts_message_content():
    """Covers both the OpenAI seat and the Fireworks/Kimi seat -- same code path."""
    fake_choice = SimpleNamespace(message=SimpleNamespace(content="hello from kimi"))
    fake_response = SimpleNamespace(choices=[fake_choice])

    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(return_value=fake_response)

    with patch("openai.AsyncOpenAI", return_value=mock_client):
        agent = OpenAICompatAgent(
            agent_id="b", model_id="some-model", family="moonshot",
            api_key_field="fireworks_api_key", base_url="https://api.fireworks.ai/inference/v1",
        )
        with patch("app.debate.panel.settings") as mock_settings:
            mock_settings.require.return_value = "fake-key"
            result = asyncio.run(agent.respond("system", "user"))

    assert result == "hello from kimi"


def test_openai_compat_agent_handles_none_content():
    """A tool-call-only response has message.content = None; must not crash."""
    fake_choice = SimpleNamespace(message=SimpleNamespace(content=None))
    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(
        return_value=SimpleNamespace(choices=[fake_choice])
    )

    with patch("openai.AsyncOpenAI", return_value=mock_client):
        agent = OpenAICompatAgent(
            agent_id="b", model_id="m", family="openai", api_key_field="openai_api_key",
        )
        with patch("app.debate.panel.settings") as mock_settings:
            mock_settings.require.return_value = "fake-key"
            result = asyncio.run(agent.respond("system", "user"))

    assert result == ""  # empty, not a crash


# --- _extract_json against realistic model-output quirks, not just clean fixtures ---

def test_extract_json_survives_trailing_commentary():
    text = '{"action": "pass", "content": "ok"}\n\nLet me know if you need anything else!'
    assert _extract_json(text)["action"] == "pass"


def test_extract_json_survives_leading_commentary_and_fence():
    text = 'Here is my response:\n\n```json\n{"action": "propose", "summary": "s"}\n```'
    assert _extract_json(text)["action"] == "propose"


def test_extract_json_handles_nested_objects_in_change_set():
    """A real change_set has nested dicts; naive first-brace/last-brace slicing must not break."""
    text = '''{"action": "propose", "change_set": {"ops": [{"op_type": "update_task_node",
              "changes": {"io_schema": {"type": "object", "properties": {"x": {"type": "string"}}}}}]}}'''
    parsed = _extract_json(text)
    assert parsed["change_set"]["ops"][0]["changes"]["io_schema"]["type"] == "object"


def test_extract_json_rejects_single_quoted_pseudo_json():
    """Python-dict-style output (single quotes) is not valid JSON and must fail loudly."""
    with pytest.raises(ValueError):
        _extract_json("{'action': 'pass'}")


def test_extract_json_picks_first_fenced_block_when_multiple_present():
    text = 'reasoning...\n```json\n{"action": "pass"}\n```\nmore text\n```\nnot json\n```'
    assert _extract_json(text)["action"] == "pass"


# --- Local provider support (development without paid API access) ---

def test_local_panel_derives_distinct_families():
    """
    Family must come from the model name, so llama3.2 and llama3.1 would
    correctly count as the same family (shared pretraining lineage) while
    llama/qwen/mistral count as distinct.
    """
    from app.config import get_settings
    from app.debate.panel import assert_heterogeneous, local_panel

    with patch("app.debate.panel.settings") as s:
        s.local_panel_models = "llama3.2,qwen2.5,mistral"
        s.local_base_url = "http://localhost:11434/v1"
        panel = local_panel()

    assert [a.family for a in panel] == ["llama", "qwen", "mistral"]
    assert_heterogeneous(panel)  # must not raise


def test_local_panel_catches_same_family_different_versions():
    """Two Llama versions share blind spots and must fail the check."""
    from app.debate.panel import assert_heterogeneous, local_panel

    with patch("app.debate.panel.settings") as s:
        s.local_panel_models = "llama3.2,llama3.1"
        s.local_base_url = "http://localhost:11434/v1"
        panel = local_panel()

    with pytest.raises(ValueError, match="not heterogeneous"):
        assert_heterogeneous(panel)


def test_local_agent_needs_no_api_key():
    """
    A local server requires no credential. settings.require() must not be
    called for it -- that would raise and make local mode unusable.
    """
    from app.debate.panel import OpenAICompatAgent

    fake_choice = SimpleNamespace(message=SimpleNamespace(content="ok"))
    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(
        return_value=SimpleNamespace(choices=[fake_choice])
    )

    agent = OpenAICompatAgent(
        agent_id="local", model_id="llama3.2", family="llama",
        api_key_field=None, base_url="http://localhost:11434/v1",
    )

    with patch("openai.AsyncOpenAI", return_value=mock_client):
        with patch("app.debate.panel.settings") as s:
            s.require.side_effect = AssertionError("require() must not be called")
            result = asyncio.run(agent.respond("sys", "user"))

    assert result == "ok"
