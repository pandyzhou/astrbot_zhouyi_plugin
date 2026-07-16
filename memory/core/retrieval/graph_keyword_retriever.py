"""Keyword retrieval for the graph-memory route."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from astrbot.api import logger

from ...storage.graph_store import GraphStore
from ..models.evolving_memory import MemoryAccessContext
from ..processors.text_processor import TextProcessor
from .access_filters import is_metadata_accessible


@dataclass(slots=True)
class GraphKeywordResult:
    """Keyword match aggregated to one source memory."""

    doc_id: int
    score: float
    content: str
    metadata: dict[str, Any]


class GraphKeywordRetriever:
    """Retrieve graph-memory candidates with FTS and one-hop expansion."""

    def __init__(
        self,
        graph_store: GraphStore,
        text_processor: TextProcessor,
        config: dict[str, Any] | None = None,
    ):
        self.graph_store = graph_store
        self.text_processor = text_processor
        self.config = config or {}
        self.expansion_limit = int(self.config.get("graph_expansion_limit", 24))
        self.expansion_hops = max(
            1,
            min(2, int(self.config.get("graph_expansion_hops", 1))),
        )
        self.second_hop_weight = float(self.config.get("graph_second_hop_weight", 0.4))

    async def _safe_route(self, route_name: str, coroutine):
        try:
            return await coroutine
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                f"[GraphKeywordRetriever] {route_name}失败，保留其他关键词子路",
                exc_info=True,
            )
            return []

    async def search(
        self,
        query: str,
        limit: int = 10,
        session_id: str | None = None,
        persona_id: str | None = None,
        access_context: MemoryAccessContext | None = None,
    ) -> list[GraphKeywordResult]:
        """Search the graph route with keyword matching."""
        if not query or not query.strip():
            return []

        tokens = await self.text_processor.tokenize_async(query, remove_stopwords=True)
        if not tokens:
            return []

        escaped_tokens = ['"' + token.replace('"', '""') + '"' for token in tokens]
        fts_query = " OR ".join(escaped_tokens)

        direct_hits, matched_nodes = await asyncio.gather(
            self._safe_route(
                "直接 FTS",
                self.graph_store.search_entries_by_bm25(
                    fts_query=fts_query,
                    limit=max(limit * 3, 12),
                    session_id=session_id,
                    persona_id=persona_id,
                ),
            ),
            self._safe_route(
                "节点匹配",
                self.graph_store.search_nodes_by_tokens(
                    tokens=tokens,
                    limit=max(limit * 3, 12),
                ),
            ),
        )
        matched_node_ids = [item["id"] for item in matched_nodes]
        expansion_hits = await self._safe_route(
            "节点条目展开",
            self.graph_store.get_entries_for_node_ids(
                node_ids=matched_node_ids,
                limit=max(self.expansion_limit, limit * 3),
                session_id=session_id,
                persona_id=persona_id,
            ),
        )
        edge_neighbor_hits: list[dict[str, Any]] = []
        second_hop_hits: list[dict[str, Any]] = []
        if matched_node_ids:
            first_hop_node_ids = await self._safe_route(
                "一跳邻居",
                self.graph_store.get_neighbor_node_ids(
                    node_ids=matched_node_ids,
                    limit=max(self.expansion_limit, limit * 3),
                ),
            )
            matched_node_set = set(matched_node_ids)
            first_hop_node_ids = [
                node_id
                for node_id in first_hop_node_ids
                if node_id not in matched_node_set
            ]
            edge_neighbor_hits = await self._safe_route(
                "一跳条目",
                self.graph_store.get_entries_for_node_ids(
                    node_ids=first_hop_node_ids,
                    limit=max(self.expansion_limit, limit * 3),
                    session_id=session_id,
                    persona_id=persona_id,
                ),
            )

            if self.expansion_hops >= 2 and first_hop_node_ids:
                second_hop_node_ids = await self._safe_route(
                    "二跳邻居",
                    self.graph_store.get_neighbor_node_ids(
                        node_ids=first_hop_node_ids,
                        limit=max(self.expansion_limit, limit * 3),
                    ),
                )
                excluded_node_ids = matched_node_set | set(first_hop_node_ids)
                second_hop_node_ids = [
                    node_id
                    for node_id in second_hop_node_ids
                    if node_id not in excluded_node_ids
                ]
                second_hop_hits = await self._safe_route(
                    "二跳条目",
                    self.graph_store.get_entries_for_node_ids(
                        node_ids=second_hop_node_ids,
                        limit=max(self.expansion_limit, limit * 3),
                        session_id=session_id,
                        persona_id=persona_id,
                    ),
                )

        aggregated: dict[int, GraphKeywordResult] = {}

        def merge_hit(hit: dict[str, Any], weight: float, match_source: str) -> None:
            hit_metadata = dict(hit.get("metadata") or {})
            if not is_metadata_accessible(
                hit_metadata,
                access_context=access_context,
                session_id=session_id,
                persona_id=persona_id,
                include_item_projection=True,
            ):
                return
            doc_id = int(hit["source_memory_id"])
            weighted_score = max(0.0, min(1.0, float(hit["score"]) * weight))
            hit_metadata["graph_match_source"] = match_source
            hit_metadata["graph_entry_type"] = hit.get("entry_type")
            hit_metadata["graph_relation_type"] = hit.get("relation_type")
            current = aggregated.get(doc_id)
            if current is None or weighted_score > current.score:
                aggregated[doc_id] = GraphKeywordResult(
                    doc_id=doc_id,
                    score=weighted_score,
                    content=str(hit.get("content") or ""),
                    metadata=hit_metadata,
                )
                return
            current.score = min(1.0, current.score + weighted_score * 0.35)
            if "graph_match_source" in current.metadata:
                current.metadata["graph_match_source"] = (
                    f"{current.metadata['graph_match_source']}+{match_source}"
                )

        for hit in direct_hits:
            merge_hit(hit, weight=1.0, match_source="graph_keyword")

        for hit in expansion_hits:
            merge_hit(hit, weight=0.7, match_source="graph_neighbor")

        for hit in edge_neighbor_hits:
            merge_hit(hit, weight=0.7, match_source="graph_edge_neighbor")

        for hit in second_hop_hits:
            merge_hit(
                hit,
                weight=max(0.0, min(1.0, self.second_hop_weight)),
                match_source="graph_second_hop",
            )

        results = sorted(aggregated.values(), key=lambda item: item.score, reverse=True)
        return results[:limit]


__all__ = ["GraphKeywordRetriever", "GraphKeywordResult"]
