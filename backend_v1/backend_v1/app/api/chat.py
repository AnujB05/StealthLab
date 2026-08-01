"""
Chat endpoint (V1 item #3).

Uses a single model rather than the debate panel -- this is a retrieval
question, not an adjudication, so panel heterogeneity buys nothing here
and would triple the cost of every message.
"""
from __future__ import annotations

import logging
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.debate.panel import AnthropicAgent
from app.services.chat import ChatService

log = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/chat", tags=["chat"])


async def get_pool(request: Request):
    return request.app.state.pool


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=6, ge=1, le=20)


class ChatResponse(BaseModel):
    answer: str
    cited_node_ids: list[UUID]
    unresolved_citations: list[str]
    groundedness: float
    retrieved_count: int
    context_empty: bool


@router.post("", response_model=ChatResponse)
async def ask(body: ChatRequest, pool=Depends(get_pool)) -> ChatResponse:
    service = ChatService(pool, AnthropicAgent(agent_id="chat"))
    try:
        result = await service.ask(body.question, top_k=body.top_k)
    except Exception as exc:  # noqa: BLE001
        # Surface the real reason (missing key, provider outage, billing)
        # rather than a generic 500 -- the same principle applied to the
        # scan endpoint's diagnostics.
        raise HTTPException(502, f"chat model call failed: {exc}") from exc

    return ChatResponse(
        answer=result.answer,
        cited_node_ids=result.cited_node_ids,
        unresolved_citations=result.unresolved_citations,
        groundedness=result.groundedness,
        retrieved_count=result.retrieved_count,
        context_empty=result.context_empty,
    )
