"""
Debate panel agents (MVP plan, Section 7).

The engine talks only to the PanelAgent protocol and never imports a
vendor SDK. That keeps the debate logic testable offline (see
MockAgent) and makes swapping a panel seat a config change.

Heterogeneity is a correctness property here, not a preference: a panel
of three instances of one model shares its blind spots, which is exactly
the failure mode debate is supposed to catch. assert_heterogeneous()
enforces it at construction rather than trusting the roster.
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any, Optional, Protocol, runtime_checkable

from app.config import settings


@runtime_checkable
class PanelAgent(Protocol):
    agent_id: str
    model_id: str
    family: str  # vendor/architecture family -- used for the heterogeneity check

    async def respond(self, system: str, user: str) -> str: ...


def _extract_json(text: str) -> dict[str, Any]:
    """
    Pull a JSON object out of a model response.

    Models wrap JSON in prose or fences despite instructions often enough
    that parsing must be defensive. Raises ValueError on genuine failure so
    the caller can record a malformed turn rather than crashing the debate.
    """
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            text = text[start : end + 1]
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"no parseable JSON object in response: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"expected a JSON object, got {type(parsed).__name__}")
    return parsed


@dataclass
class AnthropicAgent:
    agent_id: str
    model_id: str = ""
    family: str = "anthropic"
    max_tokens: int = 2000

    def __post_init__(self) -> None:
        self.model_id = self.model_id or settings.anthropic_model

    async def respond(self, system: str, user: str) -> str:
        from anthropic import AsyncAnthropic

        client = AsyncAnthropic(api_key=settings.require("anthropic_api_key"))
        msg = await client.messages.create(
            model=self.model_id,
            max_tokens=self.max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(block.text for block in msg.content if block.type == "text")


@dataclass
class OpenAICompatAgent:
    """
    Covers any OpenAI-compatible endpoint. Used for both OpenAI proper and
    Fireworks (Kimi K3), which differ only in base_url and key.
    """

    agent_id: str
    model_id: str
    family: str
    api_key_field: str
    base_url: Optional[str] = None
    max_tokens: int = 2000

    async def respond(self, system: str, user: str) -> str:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(
            api_key=settings.require(self.api_key_field), base_url=self.base_url
        )
        resp = await client.chat.completions.create(
            model=self.model_id,
            max_tokens=self.max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return resp.choices[0].message.content or ""


@dataclass
class MockAgent:
    """Scripted agent for offline tests. Returns queued responses in order."""

    agent_id: str
    responses: list[str]
    model_id: str = "mock"
    family: str = "mock"
    _index: int = 0

    async def respond(self, system: str, user: str) -> str:
        if self._index >= len(self.responses):
            return json.dumps({"action": "pass", "content": "nothing further"})
        out = self.responses[self._index]
        self._index += 1
        return out


def default_panel() -> list[PanelAgent]:
    """The v0 fixed roster (Section 7): three distinct model families."""
    return [
        AnthropicAgent(agent_id="panelist_a"),
        OpenAICompatAgent(
            agent_id="panelist_b",
            model_id=settings.fireworks_model,
            family="moonshot",
            api_key_field="fireworks_api_key",
            base_url="https://api.fireworks.ai/inference/v1",
        ),
        OpenAICompatAgent(
            agent_id="panelist_c",
            model_id=settings.openai_model,
            family="openai",
            api_key_field="openai_api_key",
        ),
    ]


def default_judge() -> PanelAgent:
    """
    The independent adjudicator (Section 7/8.1). Must not share a model
    family with any panelist -- the panel already uses all three other
    configured providers, so this is deliberately the fourth.
    """
    return OpenAICompatAgent(
        agent_id="judge",
        model_id=settings.gemini_model,
        family="google",
        api_key_field="google_api_key",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
    )


def assert_heterogeneous(agents: list[PanelAgent]) -> None:
    """
    Enforce Section 7's heterogeneity requirement at construction.

    Checked on `family` rather than `model_id` because two models from the
    same lab share pretraining lineage and therefore correlated blind
    spots, even at different sizes.
    """
    families = [a.family for a in agents]
    if len(set(families)) < len(families):
        dupes = sorted({f for f in families if families.count(f) > 1})
        raise ValueError(
            f"panel is not heterogeneous -- repeated model families: {dupes}. "
            "A panel that shares a lineage shares its blind spots."
        )


async def gather_responses(
    agents: list[PanelAgent], system: str, user: str, timeout: float = 120.0
) -> dict[str, str | Exception]:
    """
    Query agents concurrently. One agent failing (rate limit, timeout,
    outage) must not abort the round -- the exception is returned in place
    of that agent's turn and recorded as a skipped turn.
    """

    async def one(agent: PanelAgent) -> str | Exception:
        try:
            return await asyncio.wait_for(agent.respond(system, user), timeout=timeout)
        except Exception as exc:  # noqa: BLE001 -- deliberately broad
            return exc

    results = await asyncio.gather(*(one(a) for a in agents))
    return dict(zip([a.agent_id for a in agents], results))
