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
from typing import Any, Callable, Optional, Protocol, runtime_checkable

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
    Covers any OpenAI-compatible endpoint. Used for OpenAI proper,
    Fireworks (Kimi K3), Gemini's compat endpoint, and local servers
    (Ollama, LM Studio, vLLM) -- which differ only in base_url and key.

    `api_key_field=None` means no key is required, which is how local
    servers work. They still want *some* string in the header, hence the
    placeholder.
    """

    agent_id: str
    model_id: str
    family: str
    api_key_field: Optional[str] = None
    base_url: Optional[str] = None
    max_tokens: int = 2000

    async def respond(self, system: str, user: str) -> str:
        from openai import AsyncOpenAI

        key = (
            settings.require(self.api_key_field)
            if self.api_key_field
            else "not-needed-for-local"
        )
        client = AsyncOpenAI(api_key=key, base_url=self.base_url)
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


def local_panel() -> list[PanelAgent]:
    """
    Panel backed by a local OpenAI-compatible server.

    Model families are treated as distinct because they genuinely are --
    Llama, Qwen, and Mistral come from different labs with different
    pretraining corpora, so the heterogeneity argument (independent blind
    spots) holds here in the same way it does for the paid roster, even
    though each individual model is weaker.
    """
    models = [m.strip() for m in settings.local_panel_models.split(",") if m.strip()]
    return [
        OpenAICompatAgent(
            agent_id=f"panelist_{chr(97 + i)}",
            model_id=model,
            # Family derived from the model name's first token, so
            # llama3.2 and llama3.1 correctly count as the same family.
            family=model.split(":")[0].rstrip("0123456789.") or model,
            api_key_field=None,
            base_url=settings.local_base_url,
        )
        for i, model in enumerate(models)
    ]


def local_judge() -> PanelAgent:
    return OpenAICompatAgent(
        agent_id="judge",
        model_id=settings.local_judge_model,
        family=settings.local_judge_model.split(":")[0].rstrip("0123456789.")
        or settings.local_judge_model,
        api_key_field=None,
        base_url=settings.local_base_url,
    )


def _derive_family(model_name: str) -> str:
    """
    Best-effort family from a model name: everything before the first
    token that contains a digit. 'deepseek-v4-pro' and 'deepseek-v4-flash'
    both resolve to 'deepseek' (the 'v4' token stops collection before the
    tier name is ever considered), 'gpt-oss-120b' resolves to 'gpt-oss'
    (neither 'gpt' nor 'oss' contains a digit, '120b' does).

    Checking every token up to the first digit, rather than only the
    last, is the fix for the actual failure mode: a naive "does the last
    token have a digit" check misses tier names like 'pro' or 'flash'
    that follow a version token, which would otherwise make two releases
    of the same model look like different families.
    """
    name = model_name.split("/")[-1].lower()
    for cut in ("-instruct", "-chat", "-it"):
        if name.endswith(cut):
            name = name[: -len(cut)]

    tokens = name.split("-")
    family_tokens: list[str] = []
    for tok in tokens:
        if any(c.isdigit() for c in tok):
            break
        family_tokens.append(tok)

    # The whole name was version-like (e.g. a bare "qwen3.6" with no
    # separate family token) -- fall back to the first token rather than
    # returning an empty family.
    return "-".join(family_tokens) if family_tokens else tokens[0]


def general_compute_panel() -> list[PanelAgent]:
    """
    Panel backed by General Compute (hosted, open-weight, OpenAI-compatible).

    Model names come entirely from config -- there is no sensible hardcoded
    default, since General Compute's catalog and what a given account has
    access to both change. `assert_heterogeneous` still runs on whatever
    is configured, so a misconfiguration (three models that are actually
    the same family under different names) is caught at construction, not
    discovered later in a debate transcript.
    """
    models = [m.strip() for m in settings.general_compute_panel_models.split(",") if m.strip()]
    if len(models) < 3:
        raise ValueError(
            f"general_compute_panel_models must list at least 3 models, got {models!r}. "
            "Check https://docs.generalcompute.com for the current catalog."
        )
    return [
        OpenAICompatAgent(
            agent_id=f"panelist_{chr(97 + i)}",
            model_id=model,
            family=_derive_family(model),
            api_key_field="general_compute_api_key",
            base_url=settings.general_compute_base_url,
        )
        for i, model in enumerate(models)
    ]


def general_compute_judge() -> PanelAgent:
    model = settings.general_compute_judge_model
    if not model:
        raise ValueError(
            "general_compute_judge_model is not set. Pick a model whose family "
            "differs from all three panel models."
        )
    return OpenAICompatAgent(
        agent_id="judge",
        model_id=model,
        family=_derive_family(model),
        api_key_field="general_compute_api_key",
        base_url=settings.general_compute_base_url,
    )


def default_panel() -> list[PanelAgent]:
    """
    The v0 fixed roster (Section 7): three distinct model families.

    Three sources, checked in order: local (free, weakest, for structural
    smoke tests), General Compute (hosted, open-weight, cheap, real
    reasoning quality), the paid closed roster (the actually-designed
    default). Local and General Compute are mutually exclusive by
    convention -- set only one flag at a time -- checked here rather than
    silently preferring one, so a stray leftover flag doesn't quietly
    route traffic somewhere unintended.
    """
    if settings.use_local_models and settings.use_general_compute:
        raise ValueError(
            "both use_local_models and use_general_compute are set; pick one"
        )
    if settings.use_local_models:
        return local_panel()
    if settings.use_general_compute:
        return general_compute_panel()
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
    if settings.use_local_models:
        return local_judge()
    if settings.use_general_compute:
        return general_compute_judge()
    return OpenAICompatAgent(
        agent_id="judge",
        model_id=settings.gemini_model,
        family="google",
        api_key_field="google_api_key",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
    )


def default_chat_agent() -> PanelAgent:
    """
    The model that answers grounded questions in the Archive/chat surface.

    Not a debate role, so none of the heterogeneity or independence
    requirements apply here -- it's a single informational call, not an
    adversarial or adjudicative one. Reuses whichever provider is
    configured as the judge, for the same reason `default_layer2_agent`
    does: no need for a fifth distinct provider just for this.

    This exists because chat.py previously hardcoded AnthropicAgent
    directly, built before local/General Compute provider selection
    existed and never updated when it was added -- the same class of gap
    Layer 2's wiring had earlier in this project.
    """
    if settings.use_local_models:
        agent = local_judge()
    elif settings.use_general_compute:
        agent = general_compute_judge()
    else:
        agent = AnthropicAgent(agent_id="chat")
        return agent
    agent.agent_id = "chat"
    return agent


def default_layer2_agent() -> PanelAgent:
    """
    The model that estimates counterfactual outcomes for Layer 2.

    Reuses the judge's provider rather than adding a fifth: the
    independence requirement that matters is between the *adjudicator* and
    the *debaters*, and Layer 2 is neither -- it's estimating what would
    have happened, not arguing for or judging a position. Adding another
    provider here would be cost and configuration for no integrity gain.
    """
    if settings.use_local_models:
        agent = local_judge()
    elif settings.use_general_compute:
        agent = general_compute_judge()
    else:
        agent = OpenAICompatAgent(
            agent_id="layer2",
            model_id=settings.gemini_model,
            family="google",
            api_key_field="google_api_key",
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        )
    agent.agent_id = "layer2"
    return agent


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
    agents: list[PanelAgent], system: str, user: str, timeout: float = 120.0,
    on_call: Optional[Callable[[PanelAgent, str, str], "asyncio.Future"]] = None,
) -> dict[str, str | Exception]:
    """
    Query agents concurrently. One agent failing (rate limit, timeout,
    outage) must not abort the round -- the exception is returned in place
    of that agent's turn and recorded as a skipped turn.

    `on_call`, if given, fires once per agent immediately after its
    response, with (agent, prompt_text, response_text) -- the actual
    call site for cost recording, since this is the only place that sees
    every real request/response pair a debate round produces.
    """

    async def one(agent: PanelAgent) -> str | Exception:
        try:
            result = await asyncio.wait_for(agent.respond(system, user), timeout=timeout)
            if on_call:
                await on_call(agent, system + user, result)
            return result
        except Exception as exc:  # noqa: BLE001 -- deliberately broad
            return exc

    results = await asyncio.gather(*(one(a) for a in agents))
    return dict(zip([a.agent_id for a in agents], results))
