"""
Offline tests for the chat layer's citation handling (V1 item #3).

Covers the parsing/verification logic that doesn't need Voyage or a live
model. Retrieval quality itself needs real embeddings and is verified
separately against a live database.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.services.chat import CITE_PATTERN, ChatService, extract_citations
from app.services.embeddings import Embedder, EmbeddingError, node_text, to_pgvector


def test_extracts_well_formed_citations():
    a, b = uuid4(), uuid4()
    text = f"The step is slow [task:{a}] and the policy requires review [knowledge:{b}]."
    cites = extract_citations(text)
    assert len(cites) == 2
    assert cites[0] == ("task", str(a))
    assert cites[1] == ("knowledge", str(b))


def test_ignores_malformed_citation_shapes():
    """Bracketed text that isn't a citation must not be picked up."""
    assert extract_citations("[task:not-a-uuid]") == []
    assert extract_citations("[unknown:00000000-0000-0000-0000-000000000000]") == []
    assert extract_citations("see the task node") == []
    assert extract_citations("[task]") == []


def test_citation_pattern_is_case_insensitive_on_uuid_hex():
    upper = str(uuid4()).upper()
    assert len(extract_citations(f"[task:{upper}]")) == 1


def _service_with_graph(exists_map: dict):
    pool = MagicMock()
    service = ChatService(pool, MagicMock())
    graph = MagicMock()

    async def node_exists(node_id, table):
        return exists_map.get(str(node_id), False)

    graph.node_exists = node_exists
    service._graph = graph
    return service


def test_all_citations_resolving_scores_one():
    a, b = uuid4(), uuid4()
    service = _service_with_graph({str(a): True, str(b): True})
    resolved, unresolved, score = asyncio.run(
        service.verify_citations(f"one [task:{a}] two [knowledge:{b}]")
    )
    assert len(resolved) == 2
    assert unresolved == []
    assert score == 1.0


def test_hallucinated_citation_is_caught():
    """An id the model invented must be flagged, not trusted."""
    real, fake = uuid4(), uuid4()
    service = _service_with_graph({str(real): True})
    resolved, unresolved, score = asyncio.run(
        service.verify_citations(f"real [task:{real}] invented [task:{fake}]")
    )
    assert len(resolved) == 1
    assert len(unresolved) == 1
    assert score == 0.5


def test_uncited_answer_scores_zero():
    """Same rule as Layer 1: no citation means no anchor, score 0.0."""
    service = _service_with_graph({})
    resolved, unresolved, score = asyncio.run(
        service.verify_citations("This workflow generally takes about a week.")
    )
    assert resolved == []
    assert score == 0.0


def test_empty_context_short_circuits_without_calling_the_model():
    """
    With nothing retrieved there's nothing to ground against, so the model
    must not be called at all -- anything it produced would be exactly the
    unattributable general knowledge the prompt forbids.
    """
    agent = MagicMock()
    agent.respond = AsyncMock(side_effect=AssertionError("model must not be called"))

    retriever = MagicMock()
    empty = MagicMock()
    empty.nodes = []
    retriever.retrieve = AsyncMock(return_value=empty)

    service = ChatService(MagicMock(), agent, retriever)
    result = asyncio.run(service.ask("anything"))

    assert result.context_empty is True
    assert result.retrieved_count == 0
    agent.respond.assert_not_called()


def test_pgvector_serialization_format():
    assert to_pgvector([1.0, 2.5, -3.0]) == "[1.0,2.5,-3.0]"
    assert to_pgvector([]) == "[]"


def test_node_text_includes_description_when_present():
    assert node_text("Extract", "pulls fields out") == "Extract\npulls fields out"
    assert node_text("Extract") == "Extract"
    assert node_text("Extract", None) == "Extract"


def test_embedder_rejects_empty_input_without_calling_api():
    """No API call should be made for an empty batch."""
    assert asyncio.run(Embedder().embed([])) == []
