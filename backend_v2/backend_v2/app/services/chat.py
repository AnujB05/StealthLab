"""
Grounded chat over the knowledge graph (V1 item #3).

The V1 held-items doc set an explicit standard for this: answers get the
same citation discipline the debate panel already enforces -- not a
looser, separate notion of "grounded" for chat specifically. That's what
`verify_citations` does here: every cited id is checked against the real
graph, exactly as `Layer1Evaluator._groundedness` does for debate
candidates.

The distinction that matters: an LLM claiming a citation is not the same
as the citation existing. Only the database can settle that, so the
answer's `groundedness` is computed, never judged.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional
from uuid import UUID

import asyncpg

from app.db.graph_store import GraphStore
from app.debate.panel import PanelAgent
from app.services.retrieval import HybridRetriever, RetrievalResult

log = logging.getLogger(__name__)

# Matches the [task:<uuid>] / [knowledge:<uuid>] form the context uses.
CITE_PATTERN = re.compile(
    r"\[(task|knowledge):([0-9a-fA-F-]{36})\]"
)

CHAT_SYSTEM = """\
You answer questions about a company's documented workflows, using only \
the workflow context provided to you.

Rules:
- Ground every factual claim in the provided context, citing the node it \
comes from in the form [task:<id>] or [knowledge:<id>], copying ids \
exactly as they appear.
- If the context does not contain the answer, say so plainly. Do not \
fill the gap with general knowledge about how such workflows usually \
work -- an answer that sounds right but isn't drawn from this company's \
actual documented process is worse than admitting the gap, because the \
reader cannot tell the difference.
- Do not invent node ids. A citation to an id not present in the context \
will be detected and flagged.
- Be concise. Answer the question asked.
"""


@dataclass
class ChatAnswer:
    answer: str
    cited_node_ids: list[UUID] = field(default_factory=list)
    unresolved_citations: list[str] = field(default_factory=list)
    groundedness: float = 0.0
    retrieved_count: int = 0
    context_empty: bool = False


def extract_citations(text: str) -> list[tuple[str, str]]:
    return [(kind, node_id) for kind, node_id in CITE_PATTERN.findall(text)]


class ChatService:
    def __init__(
        self,
        pool: asyncpg.Pool,
        agent: PanelAgent,
        retriever: Optional[HybridRetriever] = None,
    ):
        self._pool = pool
        self._agent = agent
        self._retriever = retriever or HybridRetriever(pool)
        # Citation verification must use the same scope as retrieval:
        # verifying against an unscoped graph would confirm the existence
        # of nodes the viewer cannot see.
        self._graph = GraphStore(pool, scope=self._retriever._scope)

    async def verify_citations(self, text: str) -> tuple[list[UUID], list[str], float]:
        """
        Check every cited id against the real graph.

        Same rule as Layer 1's groundedness check: an uncited answer
        scores 0.0. It isn't rejected -- an LLM can say something true
        without citing it -- but the score surfaces on the response so
        the reader knows how much of it is anchored.
        """
        citations = extract_citations(text)
        if not citations:
            return [], [], 0.0

        resolved: list[UUID] = []
        unresolved: list[str] = []
        for kind, raw_id in citations:
            table = "task_nodes" if kind == "task" else "knowledge_nodes"
            try:
                node_id = UUID(raw_id)
            except ValueError:
                unresolved.append(f"{kind}:{raw_id}")
                continue
            try:
                exists = await self._graph.node_exists(node_id, table)  # type: ignore[arg-type]
            except Exception as exc:  # noqa: BLE001
                log.warning("citation lookup failed for %s: %s", node_id, exc)
                exists = False
            if exists:
                resolved.append(node_id)
            else:
                unresolved.append(f"{kind}:{raw_id}")

        total = len(resolved) + len(unresolved)
        return resolved, unresolved, (len(resolved) / total if total else 0.0)

    async def ask(self, question: str, top_k: int = 6) -> ChatAnswer:
        retrieval: RetrievalResult = await self._retriever.retrieve(question, top_k=top_k)

        if not retrieval.nodes:
            # Don't call the model with an empty context -- it has nothing
            # to ground against, so anything it produces would be exactly
            # the unattributable general knowledge the system prompt
            # forbids.
            return ChatAnswer(
                answer=(
                    "I couldn't find anything in the workflow graph matching that "
                    "question. It may not be documented here yet."
                ),
                retrieved_count=0,
                context_empty=True,
            )

        user_prompt = (
            f"## Workflow context\n\n{retrieval.as_context()}\n\n"
            f"## Question\n\n{question}"
        )
        try:
            raw = await self._agent.respond(CHAT_SYSTEM, user_prompt)
        except Exception as exc:  # noqa: BLE001
            log.error("chat model call failed: %s", exc)
            raise

        resolved, unresolved, score = await self.verify_citations(raw)
        return ChatAnswer(
            answer=raw,
            cited_node_ids=resolved,
            unresolved_citations=unresolved,
            groundedness=round(score, 3),
            retrieved_count=len(retrieval.nodes),
        )
