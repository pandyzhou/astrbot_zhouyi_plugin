"""Owner-scoped retrieval for revisioned evolving memory items."""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

import aiosqlite

from astrbot.api import logger

from ...storage.evolving_memory_store import EvolvingMemoryStore
from ..models.evolving_memory import MemoryAccessContext
from ..processors.text_processor import TextProcessor
from .access_filters import is_metadata_accessible
from .hybrid_retriever import HybridResult
from .vector_retriever import VectorRetriever


class EvolvingMemoryRetriever:
    """Search canonical current revisions with FTS and optional projection vectors."""

    def __init__(
        self,
        store: EvolvingMemoryStore,
        text_processor: TextProcessor,
        vector_retriever: VectorRetriever | None = None,
        config: dict[str, Any] | None = None,
    ):
        self.store = store
        self.text_processor = text_processor
        self.vector_retriever = vector_retriever
        self.config = config or {}
        self.rrf_k = max(1, int(self.config.get("rrf_k", 60)))

    @staticmethod
    def _synthetic_doc_id(memory_item_id: str) -> int:
        digest = hashlib.sha1(memory_item_id.encode("utf-8")).hexdigest()
        return -int(digest[:15], 16)

    async def _fts_query(self, query: str) -> str:
        tokens = await self.text_processor.tokenize_async(query, remove_stopwords=True)
        if not tokens:
            tokens = [token for token in query.split() if token]
        safe_tokens = []
        for token in tokens[:32]:
            cleaned = str(token).replace('"', '""').strip()
            if cleaned:
                safe_tokens.append(f'"{cleaned}"')
        return " OR ".join(safe_tokens)

    async def _search_fts(
        self,
        query: str,
        limit: int,
        context: MemoryAccessContext,
    ) -> list[dict[str, Any]]:
        fts_query = await self._fts_query(query)
        if not fts_query:
            return []
        where, params = self.store._access_clause(context)
        async with self.store._connect() as db:
            try:
                cursor = await db.execute(
                    f"""
                    SELECT mi.*, r.content AS revision_content,
                           r.structured_payload AS revision_structured_payload,
                           bm25(livingmemory_memory_items_fts) AS bm25_score
                    FROM livingmemory_memory_items_fts fts
                    JOIN memory_items mi ON mi.memory_item_id = fts.memory_item_id
                    JOIN memory_item_revisions r
                      ON r.memory_item_id = mi.memory_item_id
                     AND r.revision_no = mi.current_revision_no
                    WHERE livingmemory_memory_items_fts MATCH ?
                      AND {where}
                      AND mi.status IN ('active', 'conflicted')
                    ORDER BY bm25_score ASC, mi.updated_at DESC
                    LIMIT ?
                    """,
                    (fts_query, *params, max(1, min(limit, 500))),
                )
                rows = await cursor.fetchall()
            except asyncio.CancelledError:
                raise
            except aiosqlite.Error:
                logger.warning("[EvolvingMemoryRetriever] 对象 FTS 查询失败", exc_info=True)
                rows = []

            if not rows:
                like_tokens = list(
                    dict.fromkeys(
                        [query.strip(), *[token for token in query.split() if token]]
                    )
                )[:16]
                like_clauses = " OR ".join("r.content LIKE ?" for _ in like_tokens)
                cursor = await db.execute(
                    f"""
                    SELECT mi.*, r.content AS revision_content,
                           r.structured_payload AS revision_structured_payload,
                           0.5 AS bm25_score
                    FROM memory_items mi
                    JOIN memory_item_revisions r
                      ON r.memory_item_id = mi.memory_item_id
                     AND r.revision_no = mi.current_revision_no
                    WHERE {where}
                      AND mi.status IN ('active', 'conflicted')
                      AND ({like_clauses})
                    ORDER BY mi.updated_at DESC
                    LIMIT ?
                    """,
                    (*params, *[f"%{token}%" for token in like_tokens], max(1, min(limit, 500))),
                )
                rows = await cursor.fetchall()

        if not rows:
            return []
        raw_scores = [float(row["bm25_score"]) for row in rows]
        high = max(raw_scores)
        low = min(raw_scores)
        span = high - low
        results: list[dict[str, Any]] = []
        for row in rows:
            normalized = 1.0 if span == 0 else (high - float(row["bm25_score"])) / span
            results.append({"item": self.store._row_to_item(row), "score": normalized})
        return results

    async def _search_vector(
        self,
        query: str,
        limit: int,
        context: MemoryAccessContext,
    ) -> list[Any]:
        if self.vector_retriever is None:
            return []
        try:
            return await self.vector_retriever.search(
                query,
                limit,
                access_context=context,
                include_item_projection=True,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "[EvolvingMemoryRetriever] 对象向量路失败，降级为 FTS-only",
                exc_info=True,
            )
            return []

    async def search(
        self,
        query: str,
        k: int,
        access_context: MemoryAccessContext,
    ) -> list[HybridResult]:
        if not query or not query.strip() or k <= 0:
            return []

        candidate_limit = max(k * 4, k)
        fts_results, vector_results = await asyncio.gather(
            self._search_fts(query, candidate_limit, access_context),
            self._search_vector(query, candidate_limit, access_context),
        )

        candidates: dict[str, dict[str, Any]] = {}
        for rank, result in enumerate(fts_results, start=1):
            item = result["item"]
            candidates[item.memory_item_id] = {
                "item": item,
                "bm25_score": float(result["score"]),
                "vector_score": None,
                "rrf_score": 1.0 / (self.rrf_k + rank),
            }

        vector_metadata: dict[str, tuple[Any, int]] = {}
        for rank, result in enumerate(vector_results, start=1):
            metadata = result.metadata if isinstance(result.metadata, dict) else {}
            if not is_metadata_accessible(
                metadata,
                access_context=access_context,
                include_item_projection=True,
            ):
                continue
            item_id = str(metadata.get("memory_item_id") or "").strip()
            if item_id:
                vector_metadata[item_id] = (result, rank)

        if vector_metadata:
            placeholders = ",".join("?" for _ in vector_metadata)
            async with self.store._connect() as db:
                cursor = await db.execute(
                    f"""
                    SELECT mi.*, r.content AS revision_content,
                           r.structured_payload AS revision_structured_payload
                    FROM memory_items mi
                    JOIN memory_item_revisions r
                      ON r.memory_item_id = mi.memory_item_id
                     AND r.revision_no = mi.current_revision_no
                    WHERE mi.owner_user_id = ?
                      AND mi.memory_item_id IN ({placeholders})
                      AND mi.status IN ('active', 'conflicted')
                    """,
                    (access_context.owner_user_id, *vector_metadata.keys()),
                )
                rows = await cursor.fetchall()
            for row in rows:
                item = self.store._row_to_item(row)
                vector_result, rank = vector_metadata[item.memory_item_id]
                if item.current_document_id != vector_result.doc_id:
                    continue
                current = candidates.setdefault(
                    item.memory_item_id,
                    {
                        "item": item,
                        "bm25_score": None,
                        "vector_score": None,
                        "rrf_score": 0.0,
                    },
                )
                current["vector_score"] = float(vector_result.score)
                current["rrf_score"] += 1.0 / (self.rrf_k + rank)

        if not candidates:
            return []
        max_rrf = max(float(value["rrf_score"]) for value in candidates.values()) or 1.0
        results: list[HybridResult] = []
        for value in candidates.values():
            item = value["item"]
            normalized_rrf = float(value["rrf_score"]) / max_rrf
            final_score = min(
                1.0,
                0.7 * normalized_rrf
                + 0.2 * float(item.importance)
                + 0.1 * float(item.confidence),
            )
            metadata = {
                "archive_type": "memory_item",
                "memory_item_id": item.memory_item_id,
                "memory_revision_no": item.current_revision_no,
                "owner_user_id": item.owner_user_id,
                "scope": item.scope.value,
                "session_id": item.session_id,
                "persona_id": item.persona_id,
                "group_safe": item.group_safe,
                "item_status": item.status.value,
                "item_type": item.item_type,
                "importance": item.importance,
                "confidence": item.confidence,
                "current_document_id": item.current_document_id,
                "version": item.version,
            }
            results.append(
                HybridResult(
                    doc_id=item.current_document_id
                    or self._synthetic_doc_id(item.memory_item_id),
                    final_score=final_score,
                    rrf_score=float(value["rrf_score"]),
                    bm25_score=value["bm25_score"],
                    vector_score=value["vector_score"],
                    content=item.content,
                    metadata=metadata,
                    score_breakdown={
                        "item_rrf_normalized": round(normalized_rrf, 4),
                        "item_importance": round(float(item.importance), 4),
                        "item_confidence": round(float(item.confidence), 4),
                        "item_final_score": round(final_score, 4),
                    },
                    memory_item_id=item.memory_item_id,
                    version=item.version,
                    source_type="memory_item",
                )
            )
        results.sort(key=lambda result: result.final_score, reverse=True)
        return results[:k]


__all__ = ["EvolvingMemoryRetriever"]
