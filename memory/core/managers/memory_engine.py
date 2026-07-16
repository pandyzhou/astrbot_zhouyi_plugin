"""
统一记忆引擎 - MemoryEngine
提供统一的记忆管理接口,整合所有底层组件
"""

import asyncio
import copy
import json
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

import aiosqlite

from astrbot.api import logger

from ...storage.atom_store import AtomStore
from ...storage.evolving_memory_store import EvolvingMemoryStore
from ...storage.graph_store import GraphStore
from ..managers.atom_lifecycle_manager import AtomLifecycleManager
from ..managers.evolving_memory_manager import EvolvingMemoryManager
from ..managers.graph_memory_manager import GraphMemoryManager
from ..models.evolving_memory import (
    MemoryAccessContext,
    MemoryItemStatus,
    MutationResult,
)
from ..models.memory_atom import AtomStatus, AtomType, DecayType, MemoryAtom
from ..processors.graph_extractor import GraphExtractor
from ..processors.text_processor import TextProcessor
from ..retrieval.atom_retriever import AtomRetriever
from ..retrieval.bm25_retriever import BM25Retriever
from ..retrieval.dual_route_retriever import DualRouteRetriever
from ..retrieval.evolving_memory_retriever import EvolvingMemoryRetriever
from ..retrieval.graph_keyword_retriever import GraphKeywordRetriever
from ..retrieval.graph_retriever import GraphRetriever
from ..retrieval.graph_vector_retriever import GraphVectorRetriever
from ..retrieval.hybrid_retriever import HybridResult, HybridRetriever
from ..retrieval.rrf_fusion import RRFFusion
from ..retrieval.vector_retriever import VectorRetriever
from ..utils.number_utils import clamp_float, safe_float


class MemoryEngine:
    """
    统一记忆引擎

    整合BM25检索、向量检索和混合检索,提供完整的记忆管理接口。

    主要功能:
    1. 记忆CRUD操作(添加、检索、更新、删除)
    2. 自动化记忆整理和清理
    3. 重要性评估和时间衰减
    4. 会话隔离和统计

    ID管理体系说明：
    ==================
    本系统使用三层存储架构，统一使用整数ID作为主键：

    1. **DocumentStorage (FAISS内部)**
       - 表: documents (SQLite，由SQLAlchemy管理)
       - 主键: id (INTEGER, AUTOINCREMENT) - 这是统一的整数标识符
       - UUID字段: doc_id (TEXT) - FAISS内部使用的UUID字符串
       - 关系: id ←→ doc_id (一对一映射)

    2. **BM25 FTS5索引**
       - 表: livingmemory_memories_fts (SQLite FTS5虚拟表)
       - 字段: doc_id (UNINDEXED) - 引用documents.id的整数
       - 注意: 只存储分词后的内容，metadata从documents表读取

    3. **FAISS向量索引**
       - 存储: EmbeddingStorage (FAISS索引文件)
       - 索引ID: 使用documents.id作为向量的整数索引

    插件对外接口：
    - add_memory() 返回: int (documents.id)
    - search_memories() 返回: HybridResult包含doc_id (int)
    - update_memory(memory_id: int) 参数: documents.id
    - delete_memory(memory_id: int) 参数: documents.id

    同步保证：
    - 添加: 先插入DocumentStorage获取id，再用此id插入BM25和FAISS
    - 更新: 通过vector_retriever更新DocumentStorage (自动同步)
    - 删除: 先删除BM25，再通过FaissVecDB.delete()删除DocumentStorage和向量
    """

    def __init__(
        self,
        db_path: str,
        faiss_db,
        graph_vector_db=None,
        llm_provider=None,
        config: dict[str, Any] | None = None,
        evolving_memory_store: EvolvingMemoryStore | None = None,
        evolving_memory_manager: EvolvingMemoryManager | None = None,
    ):
        """
        初始化记忆引擎

        Args:
            db_path: SQLite数据库路径
            faiss_db: FAISS向量数据库实例
            llm_provider: LLM提供者(可选,用于高级功能)
            config: 配置字典,支持以下参数:
                - rrf_k: RRF参数,默认60
                - decay_rate: 时间衰减率,默认0.01
                - importance_weight: 重要性权重,默认1.0
                - fallback_enabled: 启用退化机制,默认True
                - cleanup_days_threshold: 清理天数阈值,默认30
                - cleanup_importance_threshold: 清理重要性阈值,默认0.3
                - stopwords_path: 停用词文件路径(可选)
        """
        self.db_path = db_path
        self.faiss_db = faiss_db
        self.graph_vector_db = graph_vector_db
        self.llm_provider = llm_provider
        self.config = config or {}
        self.evolving_memory_store = evolving_memory_store
        self.evolving_memory_manager = evolving_memory_manager
        self.evolving_memory_retriever: EvolvingMemoryRetriever | None = None
        self._evolving_generation = 0
        self.graph_enabled = bool(self.config.get("graph_memory_enabled", False))
        self.atom_enabled = bool(
            self.config.get(
                "atom_enabled",
                self.config.get("graph_memory_atom_enabled", True),
            )
        )

        # 确保数据库目录存在
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

        # 后台任务跟踪
        self._pending_tasks: set[asyncio.Task] = set()

        # 初始化组件(在initialize中完成)
        self.text_processor = None
        self.bm25_retriever = None
        self.vector_retriever = None
        self.rrf_fusion = None
        self.hybrid_retriever = None
        self.graph_store = None
        self.graph_extractor = None
        self.graph_keyword_retriever = None
        self.graph_vector_retriever = None
        self.graph_retriever = None
        self.graph_memory_manager = None
        self.dual_route_retriever = None
        self.atom_store = None
        self.atom_lifecycle_manager = None
        self.atom_retriever = None
        self.db_connection = None
        self._search_cache_enabled = bool(self.config.get("search_cache_enabled", True))
        self._search_cache_ttl = float(
            self.config.get("search_cache_ttl_seconds", 45.0)
        )
        self._search_cache_max_size = int(self.config.get("search_cache_max_size", 256))
        self._search_cache_generation = 0
        self._search_cache: OrderedDict[
            tuple[Any, ...], tuple[float, list[HybridResult]]
        ] = OrderedDict()
        self._write_op_repair_enabled = bool(
            self.config.get("write_op_repair_enabled", True)
        )
        self._write_op_max_retries = int(self.config.get("write_op_max_retries", 3))

    async def initialize(self):
        """
        异步初始化引擎

        创建数据库表、初始化所有检索器组件
        """
        # 1. 连接数据库
        self.db_connection = await aiosqlite.connect(self.db_path)
        self.db_connection.row_factory = aiosqlite.Row
        await self.db_connection.execute("PRAGMA journal_mode = WAL")
        await self.db_connection.execute("PRAGMA busy_timeout = 10000")

        # 2. 创建表结构
        await self._create_tables()

        # 3. 初始化文本处理器
        stopwords_path = self.config.get("stopwords_path")
        self.text_processor = TextProcessor(stopwords_path)

        # 4. 初始化RRF融合器
        rrf_k = self.config.get("rrf_k", 60)
        self.rrf_fusion = RRFFusion(k=rrf_k)

        # 5. 初始化BM25检索器
        self.bm25_retriever = BM25Retriever(
            self.db_path, self.text_processor, self.config
        )
        await self.bm25_retriever.initialize()

        # 6. 向量路是可选能力；没有 Embedding 时保持纯 SQLite/BM25。
        self.vector_retriever = (
            VectorRetriever(self.faiss_db, self.config)
            if self.faiss_db is not None
            else None
        )

        # 7. 初始化文档混合检索器（支持 BM25-only）。
        self.hybrid_retriever = HybridRetriever(
            self.bm25_retriever, self.vector_retriever, self.rrf_fusion, self.config
        )

        if self.evolving_memory_store is not None:
            self.evolving_memory_retriever = EvolvingMemoryRetriever(
                self.evolving_memory_store,
                self.text_processor,
                self.vector_retriever,
                self.config,
            )
        if self.evolving_memory_manager is not None:
            self.evolving_memory_manager.projection_callback = (
                self.project_evolving_memory_mutation
            )

        if self.graph_enabled:
            self.graph_store = GraphStore(self.db_path)
            await self.graph_store.initialize()

            self.atom_store = AtomStore(self.db_path)
            await self.atom_store.initialize()

            if self.atom_enabled:
                self.atom_lifecycle_manager = AtomLifecycleManager(
                    self.atom_store, self.config
                )
                self.atom_retriever = AtomRetriever(self.atom_store, self.config)
                await self.atom_lifecycle_manager.start()

            self.graph_extractor = GraphExtractor(self.config)
            self.graph_keyword_retriever = GraphKeywordRetriever(
                self.graph_store,
                self.text_processor,
                self.config,
            )
            self.graph_vector_retriever = (
                GraphVectorRetriever(self.graph_vector_db, self.config)
                if self.graph_vector_db is not None
                else None
            )
            self.graph_retriever = GraphRetriever(
                self.graph_keyword_retriever,
                self.graph_vector_retriever,
                self.rrf_fusion,
                self.config,
            )
            self.graph_memory_manager = GraphMemoryManager(
                self.graph_store,
                self.graph_vector_retriever,
                self.graph_extractor,
            )
            self.dual_route_retriever = DualRouteRetriever(
                self.hybrid_retriever,
                self.graph_retriever,
                self.get_memory,
                self.config,
            )

        if self._write_op_repair_enabled:
            await self._repair_incomplete_write_ops()

    async def close(self):
        """关闭数据库连接和清理资源"""
        if self.atom_lifecycle_manager is not None:
            await self.atom_lifecycle_manager.stop()
        if self._pending_tasks:
            for task in self._pending_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*self._pending_tasks, return_exceptions=True)
            self._pending_tasks.clear()
        if self.db_connection:
            await self.db_connection.close()
        if self.graph_vector_db is not None:
            await self.graph_vector_db.close()

    def _create_tracked_task(self, coro) -> None:
        """Create and track a background task, auto-discarding on completion."""
        task = asyncio.create_task(coro)
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)

    async def _create_write_ops_table(self) -> None:
        """Create the resumable write-operation log."""
        if self.db_connection is None:
            return
        await self.db_connection.execute("""
            CREATE TABLE IF NOT EXISTS memory_write_ops (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                op_type TEXT NOT NULL,
                memory_id INTEGER,
                status TEXT NOT NULL DEFAULT 'pending',
                step TEXT NOT NULL DEFAULT 'started',
                payload TEXT DEFAULT '{}',
                error TEXT,
                retry_count INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        await self.db_connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_memory_write_ops_status
            ON memory_write_ops(status, updated_at)
        """)
        await self.db_connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_memory_write_ops_memory
            ON memory_write_ops(memory_id, op_type)
        """)

    async def _start_write_op(
        self,
        op_type: str,
        payload: dict[str, Any] | None = None,
        memory_id: int | None = None,
    ) -> int | None:
        """Record the beginning of a multi-store write operation."""
        if self.db_connection is None:
            return None
        now = time.time()
        try:
            cursor = await self.db_connection.execute(
                """
                INSERT INTO memory_write_ops(
                    op_type, memory_id, status, step, payload,
                    created_at, updated_at
                ) VALUES (?, ?, 'pending', 'started', ?, ?, ?)
                """,
                (
                    op_type,
                    memory_id,
                    json.dumps(payload or {}, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            await self.db_connection.commit()
            return int(cursor.lastrowid)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("[MemoryEngine] 写操作日志创建失败", exc_info=True)
            return None

    async def _advance_write_op(
        self,
        op_id: int | None,
        step: str,
        *,
        status: str = "pending",
        memory_id: int | None = None,
        error: str | None = None,
        payload_patch: dict[str, Any] | None = None,
    ) -> None:
        """Advance a write-operation log entry."""
        if op_id is None or self.db_connection is None:
            return

        try:
            if status == "completed":
                error = None
            current_payload: dict[str, Any] = {}
            if payload_patch:
                cursor = await self.db_connection.execute(
                    "SELECT payload FROM memory_write_ops WHERE id = ?",
                    (op_id,),
                )
                row = await cursor.fetchone()
                if row and row[0]:
                    try:
                        loaded = json.loads(row[0])
                        current_payload = loaded if isinstance(loaded, dict) else {}
                    except (json.JSONDecodeError, TypeError):
                        current_payload = {}
                current_payload.update(payload_patch)

            fields = ["status = ?", "step = ?", "updated_at = ?"]
            params: list[Any] = [status, step, time.time()]
            if memory_id is not None:
                fields.append("memory_id = ?")
                params.append(memory_id)
            if error is not None:
                fields.append("error = ?")
                params.append(error[:1000])
                if status != "completed":
                    fields.append("retry_count = retry_count + 1")
            elif status == "completed":
                fields.append("error = NULL")
            if payload_patch:
                fields.append("payload = ?")
                params.append(json.dumps(current_payload, ensure_ascii=False))
            params.append(op_id)
            await self.db_connection.execute(
                f"UPDATE memory_write_ops SET {', '.join(fields)} WHERE id = ?",
                params,
            )
            await self.db_connection.commit()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("[MemoryEngine] 写操作日志更新失败", exc_info=True)

    def _normalize_cache_query(self, query: str) -> str:
        return " ".join(query.casefold().split())

    def _search_cache_key(
        self,
        query: str,
        k: int,
        session_id: str | None,
        persona_id: str | None,
        access_context: MemoryAccessContext | None = None,
        object_generation: tuple[int, int, str] | None = None,
    ) -> tuple[Any, ...]:
        access_policy = (
            access_context.owner_user_id,
            tuple(sorted(scope.value for scope in access_context.allowed_scopes)),
            access_context.persona_id or "",
            access_context.session_id,
            bool(access_context.is_group),
            bool(access_context.allow_public),
            bool(access_context.allow_legacy_session),
        ) if access_context is not None else ("legacy",)
        return (
            self._search_cache_generation,
            self._evolving_generation,
            object_generation or (0, 0, ""),
            access_policy,
            self._normalize_cache_query(query),
            int(k),
            session_id or "",
            persona_id or "",
            bool(self.dual_route_retriever is not None),
            round(float(self.config.get("document_route_weight", 0.65)), 4),
            round(float(self.config.get("graph_route_weight", 0.35)), 4),
            int(self.config.get("graph_expansion_hops", 1)),
        )

    def _get_cached_search_results(
        self,
        cache_key: tuple[Any, ...],
    ) -> list[HybridResult] | None:
        if (
            not self._search_cache_enabled
            or self._search_cache_ttl <= 0
            or self._search_cache_max_size <= 0
        ):
            return None

        cached = self._search_cache.get(cache_key)
        if cached is None:
            return None

        cached_at, results = cached
        if time.time() - cached_at > self._search_cache_ttl:
            self._search_cache.pop(cache_key, None)
            return None

        self._search_cache.move_to_end(cache_key)
        return copy.deepcopy(results)

    def _set_cached_search_results(
        self,
        cache_key: tuple[Any, ...],
        results: list[HybridResult],
    ) -> None:
        if (
            not self._search_cache_enabled
            or self._search_cache_ttl <= 0
            or self._search_cache_max_size <= 0
        ):
            return

        self._search_cache[cache_key] = (time.time(), copy.deepcopy(results))
        self._search_cache.move_to_end(cache_key)
        while len(self._search_cache) > self._search_cache_max_size:
            self._search_cache.popitem(last=False)

    def _invalidate_search_cache(self) -> None:
        """Invalidate cached retrieval results after memory writes."""
        self._search_cache_generation += 1
        self._search_cache.clear()

    def _serialize_atom_for_repair(self, atom: Any) -> dict[str, Any]:
        """Convert a MemoryAtom-like object into JSON-safe repair payload."""
        atom_type = getattr(atom, "atom_type", AtomType.UNKNOWN)
        decay_type = getattr(atom, "decay_type", DecayType.EXPONENTIAL)
        status = getattr(atom, "status", AtomStatus.ACTIVE)
        return {
            "parent_memory_id": int(getattr(atom, "parent_memory_id", 0) or 0),
            "atom_type": getattr(atom_type, "value", str(atom_type)),
            "content": str(getattr(atom, "content", "")),
            "entities": list(getattr(atom, "entities", []) or []),
            "importance": float(getattr(atom, "importance", 0.5) or 0.5),
            "confidence": float(getattr(atom, "confidence", 0.7) or 0.7),
            "created_at": float(
                getattr(atom, "created_at", time.time()) or time.time()
            ),
            "last_accessed_at": float(
                getattr(atom, "last_accessed_at", time.time()) or time.time()
            ),
            "last_reinforced_at": getattr(atom, "last_reinforced_at", None),
            "event_time": getattr(atom, "event_time", None),
            "ttl_days": float(getattr(atom, "ttl_days", 30.0) or 30.0),
            "expires_at": float(getattr(atom, "expires_at", 0.0) or 0.0),
            "status": getattr(status, "value", str(status)),
            "reinforcement_count": int(getattr(atom, "reinforcement_count", 0) or 0),
            "decay_type": getattr(decay_type, "value", str(decay_type)),
            "session_id": getattr(atom, "session_id", None),
            "persona_id": getattr(atom, "persona_id", None),
            "metadata": dict(getattr(atom, "metadata", {}) or {}),
            "memory_item_id": getattr(atom, "memory_item_id", None),
            "memory_revision_no": getattr(atom, "memory_revision_no", None),
        }

    def _deserialize_atom_from_repair(
        self,
        payload: dict[str, Any],
        parent_memory_id: int,
        session_id: str | None,
        persona_id: str | None,
    ) -> MemoryAtom | None:
        """Rebuild a MemoryAtom from repair payload."""
        content = str(payload.get("content") or "")
        if not content.strip():
            return None

        try:
            atom_type = AtomType(payload.get("atom_type") or AtomType.UNKNOWN.value)
        except ValueError:
            atom_type = AtomType.UNKNOWN
        try:
            decay_type = DecayType(
                payload.get("decay_type") or DecayType.EXPONENTIAL.value
            )
        except ValueError:
            decay_type = DecayType.EXPONENTIAL
        try:
            status = AtomStatus(payload.get("status") or AtomStatus.ACTIVE.value)
        except ValueError:
            status = AtomStatus.ACTIVE

        return MemoryAtom(
            parent_memory_id=parent_memory_id,
            atom_type=atom_type,
            content=content,
            entities=[str(item) for item in payload.get("entities", []) if item],
            importance=float(payload.get("importance", 0.5) or 0.5),
            confidence=float(payload.get("confidence", 0.7) or 0.7),
            created_at=float(payload.get("created_at", time.time()) or time.time()),
            last_accessed_at=float(
                payload.get("last_accessed_at", time.time()) or time.time()
            ),
            last_reinforced_at=payload.get("last_reinforced_at"),
            event_time=payload.get("event_time"),
            ttl_days=float(payload.get("ttl_days", 30.0) or 30.0),
            expires_at=float(payload.get("expires_at", 0.0) or 0.0),
            status=status,
            reinforcement_count=int(payload.get("reinforcement_count", 0) or 0),
            decay_type=decay_type,
            session_id=payload.get("session_id") or session_id,
            persona_id=payload.get("persona_id") or persona_id,
            metadata=dict(payload.get("metadata") or {}),
            memory_item_id=payload.get("memory_item_id"),
            memory_revision_no=(
                int(payload["memory_revision_no"])
                if payload.get("memory_revision_no") is not None
                else None
            ),
        )

    async def _repair_incomplete_write_ops(self) -> int:
        """Best-effort replay for incomplete add/delete operations."""
        if self.db_connection is None:
            return 0

        try:
            cursor = await self.db_connection.execute(
                """
                SELECT id, op_type, memory_id, status, step, payload, retry_count
                FROM memory_write_ops
                WHERE status IN ('pending', 'needs_repair')
                  AND retry_count < ?
                ORDER BY id ASC
                LIMIT 25
                """,
                (self._write_op_max_retries,),
            )
            rows = await cursor.fetchall()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("[MemoryEngine] 读取待修复写操作失败", exc_info=True)
            return 0

        repaired = 0
        for row in rows:
            payload = self._safe_json_dict(row["payload"])
            try:
                op_type = row["op_type"]
                memory_id = row["memory_id"]
                if op_type == "add":
                    ok = await self._repair_add_write_op(
                        int(row["id"]),
                        int(memory_id) if memory_id is not None else None,
                        payload,
                    )
                elif op_type == "delete":
                    ok = await self._repair_delete_write_op(
                        int(row["id"]),
                        int(memory_id) if memory_id is not None else None,
                    )
                elif op_type == "batch_delete":
                    ok = await self._repair_batch_delete_write_op(
                        int(row["id"]),
                        payload,
                    )
                else:
                    ok = False
                repaired += 1 if ok else 0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(
                    f"[MemoryEngine] 修复写操作失败 (op_id={row['id']})",
                    exc_info=True,
                )
                await self._advance_write_op(
                    int(row["id"]),
                    str(row["step"] or "repair_failed"),
                    status="needs_repair",
                    error=str(e),
                )

        if repaired:
            logger.info(f"[MemoryEngine] 已修复 {repaired} 个未完成写操作")
            self._invalidate_search_cache()
        return repaired

    async def _repair_add_write_op(
        self,
        op_id: int,
        memory_id: int | None,
        payload: dict[str, Any],
    ) -> bool:
        if memory_id is None:
            await self._advance_write_op(
                op_id,
                "unrepairable",
                status="failed",
                error="missing memory_id for add repair",
            )
            return False

        memory = await self.get_memory(int(memory_id))
        if memory is None:
            await self._advance_write_op(
                op_id,
                "source_missing",
                status="failed",
                memory_id=int(memory_id),
                error="source document missing",
            )
            return False

        metadata = memory.get("metadata") or payload.get("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = self._safe_json_dict(metadata)
        content = str(memory.get("text") or "")
        session_id = metadata.get("session_id") or payload.get("session_id")
        persona_id = metadata.get("persona_id") or payload.get("persona_id")

        atom_payloads = payload.get("failed_atoms") or payload.get("atoms", []) or []
        atoms: list[MemoryAtom] = []
        for atom_payload in atom_payloads:
            if isinstance(atom_payload, dict):
                atom = self._deserialize_atom_from_repair(
                    atom_payload,
                    int(memory_id),
                    session_id,
                    persona_id,
                )
                if atom is not None:
                    atoms.append(atom)

        if self.atom_store is not None and atoms and self.atom_enabled:
            existing_atoms = await self.atom_store.get_by_parent(int(memory_id))
            if payload.get("failed_atoms"):
                existing_keys = {
                    (
                        atom.content,
                        atom.atom_type.value,
                        atom.session_id,
                        atom.persona_id,
                    )
                    for atom in existing_atoms
                }
                atoms_to_insert = [
                    atom
                    for atom in atoms
                    if (
                        atom.content,
                        atom.atom_type.value,
                        atom.session_id,
                        atom.persona_id,
                    )
                    not in existing_keys
                ]
                if atoms_to_insert:
                    await self.atom_store.insert_many(atoms_to_insert)
            elif not existing_atoms:
                await self.atom_store.insert_many(atoms)
            await self._advance_write_op(op_id, "atoms_repaired", memory_id=memory_id)

        if self.graph_memory_manager is not None and content.strip():
            await self.graph_memory_manager.index_memory(
                int(memory_id),
                content,
                metadata,
                atoms or None,
            )
            await self._advance_write_op(op_id, "graph_repaired", memory_id=memory_id)

        await self._advance_write_op(
            op_id,
            "completed",
            status="completed",
            memory_id=int(memory_id),
        )
        return True

    async def _repair_delete_write_op(
        self,
        op_id: int,
        memory_id: int | None,
    ) -> bool:
        if memory_id is None:
            await self._advance_write_op(
                op_id,
                "unrepairable",
                status="failed",
                error="missing memory_id for delete repair",
            )
            return False

        if self.graph_memory_manager is not None:
            await self.graph_memory_manager.delete_memory(int(memory_id))
        if self.atom_store is not None:
            await self.atom_store.delete_by_parent(int(memory_id))

        await self._advance_write_op(
            op_id,
            "completed",
            status="completed",
            memory_id=int(memory_id),
        )
        return True

    async def _repair_batch_delete_write_op(
        self,
        op_id: int,
        payload: dict[str, Any],
    ) -> bool:
        memory_ids_raw = payload.get("memory_ids") or []
        if not isinstance(memory_ids_raw, list):
            await self._advance_write_op(
                op_id,
                "unrepairable",
                status="failed",
                error="missing memory_ids for batch delete repair",
            )
            return False

        memory_ids: list[int] = []
        for raw_id in memory_ids_raw:
            try:
                memory_ids.append(int(raw_id))
            except (TypeError, ValueError):
                continue

        if not memory_ids:
            await self._advance_write_op(
                op_id,
                "unrepairable",
                status="failed",
                error="empty memory_ids for batch delete repair",
            )
            return False

        await self._delete_document_indexes_for_batch(memory_ids)
        await self._delete_graph_and_atoms_for_batch(memory_ids)
        await self._advance_write_op(
            op_id,
            "completed",
            status="completed",
            payload_patch={"deleted_count": len(memory_ids)},
        )
        return True

    async def _delete_document_indexes_for_batch(self, memory_ids: list[int]) -> int:
        if not memory_ids or self.db_connection is None:
            return 0

        placeholders = ",".join("?" * len(memory_ids))
        await self.db_connection.execute(
            f"DELETE FROM livingmemory_memories_fts WHERE doc_id IN ({placeholders})",
            memory_ids,
        )

        cursor = await self.db_connection.execute(
            f"SELECT id, doc_id FROM documents WHERE id IN ({placeholders})",
            memory_ids,
        )
        uuid_rows = await cursor.fetchall()
        for row in uuid_rows:
            uuid_doc_id = row["doc_id"]
            if not uuid_doc_id or self.faiss_db is None:
                continue
            try:
                await self.faiss_db.delete(uuid_doc_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    f"[批量删除] FAISS 删除失败 (id={row['id']})",
                    exc_info=True,
                )

        cursor = await self.db_connection.execute(
            f"DELETE FROM documents WHERE id IN ({placeholders})",
            memory_ids,
        )
        await self.db_connection.commit()
        return int(cursor.rowcount or 0)

    async def _delete_graph_and_atoms_for_batch(self, memory_ids: list[int]) -> None:
        if not memory_ids:
            return
        if self.graph_memory_manager is not None:
            await self.graph_memory_manager.batch_delete_memories(memory_ids)
        if self.atom_store is not None:
            await self.atom_store.batch_delete_by_parent(memory_ids)

    @staticmethod
    def _safe_json_dict(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        if not value:
            return {}
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                return parsed if isinstance(parsed, dict) else {}
            except (json.JSONDecodeError, TypeError):
                return {}
        return {}

    async def _create_tables(self):
        """创建数据库表

        注意：documents 表主要由 FAISS 的 DocumentStorage 类创建和管理。
        这里使用 CREATE TABLE IF NOT EXISTS 确保兼容性：
        - 如果 FAISS 已创建，不会重复创建（IF NOT EXISTS）
        - 如果 FAISS 未创建（极端情况），插件仍能正常工作
        - 插件需要直接操作此表进行高频更新（如访问时间）
        """
        # documents表 - 与FAISS共享，IF NOT EXISTS确保不重复创建
        if self.db_connection is not None:
            await self._drop_legacy_documents_fts_triggers()

            await self.db_connection.execute("""
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                doc_id TEXT,
                text TEXT NOT NULL,
                metadata TEXT DEFAULT '{}',
                created_at TEXT,
                updated_at TEXT
            )
        """)

            # 兼容旧版插件创建的简化 documents 表，确保 FAISS DocumentStorage 所需字段存在
            cursor = await self.db_connection.execute("PRAGMA table_info(documents)")
            column_rows = await cursor.fetchall()
            existing_columns = {row[1] for row in column_rows}

            missing_columns = []
            if "doc_id" not in existing_columns:
                await self.db_connection.execute(
                    "ALTER TABLE documents ADD COLUMN doc_id TEXT"
                )
                missing_columns.append("doc_id")
            if "created_at" not in existing_columns:
                await self.db_connection.execute(
                    "ALTER TABLE documents ADD COLUMN created_at TEXT"
                )
                missing_columns.append("created_at")
            if "updated_at" not in existing_columns:
                await self.db_connection.execute(
                    "ALTER TABLE documents ADD COLUMN updated_at TEXT"
                )
                missing_columns.append("updated_at")

            if missing_columns:
                logger.warning(
                    "[MemoryEngine] 检测到旧版 documents 表结构，已补齐字段: "
                    f"{', '.join(missing_columns)}"
                )

            # 回填旧数据，避免 doc_id/timestamp 缺失导致删除与展示异常
            await self.db_connection.execute("""
            UPDATE documents
            SET doc_id = 'legacy-' || id
            WHERE doc_id IS NULL OR TRIM(doc_id) = ''
        """)
            await self.db_connection.execute("""
            UPDATE documents
            SET created_at = datetime('now')
            WHERE created_at IS NULL OR TRIM(CAST(created_at AS TEXT)) = ''
        """)
            await self.db_connection.execute("""
            UPDATE documents
            SET updated_at = COALESCE(created_at, datetime('now'))
            WHERE updated_at IS NULL OR TRIM(CAST(updated_at AS TEXT)) = ''
        """)

            # 创建索引以提升session_id查询性能
            await self.db_connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_doc_metadata
            ON documents(json_extract(metadata, '$.session_id'))
        """)
            await self.db_connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_doc_persona_metadata
            ON documents(json_extract(metadata, '$.persona_id'))
        """)
            await self.db_connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_doc_importance_metadata
            ON documents(json_extract(metadata, '$.importance'))
        """)
            await self.db_connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_doc_last_access_metadata
            ON documents(json_extract(metadata, '$.last_access_time'))
        """)
            await self.db_connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_documents_doc_id
            ON documents(doc_id)
        """)

            await self._create_write_ops_table()

            # 创建版本管理表
            await self.db_connection.execute("""
            CREATE TABLE IF NOT EXISTS db_version (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                version INTEGER NOT NULL,
                description TEXT,
                migrated_at TEXT NOT NULL,
                migration_duration_seconds REAL
            )
        """)

            # 创建迁移状态表
            await self.db_connection.execute("""
            CREATE TABLE IF NOT EXISTS migration_status (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TEXT
            )
        """)

            await self.db_connection.commit()

            # 检查是否需要初始化版本信息
            cursor = await self.db_connection.execute("SELECT COUNT(*) FROM db_version")
            version_result = await cursor.fetchone()
            version_count = version_result[0] if version_result else 0

            if version_count == 0:
                # 全新数据库，设置初始版本为最新迁移版本
                from datetime import datetime, timezone

                from ...storage.db_migration import DBMigration

                await self.db_connection.execute(
                    """
                    INSERT INTO db_version (version, description, migrated_at, migration_duration_seconds)
                    VALUES (?, ?, ?, ?)
                """,
                    (
                        DBMigration.CURRENT_VERSION,
                        "初始版本 - 当前架构",
                        datetime.now(timezone.utc).isoformat(),
                        0.0,
                    ),
                )
                await self.db_connection.commit()

                logger.info(f"已初始化数据库版本信息: v{DBMigration.CURRENT_VERSION}")

    async def _drop_legacy_documents_fts_triggers(self):
        if self.db_connection is None:
            return

        cursor = await self.db_connection.execute("""
            SELECT name FROM sqlite_master
            WHERE type='trigger' AND tbl_name='documents'
              AND sql LIKE '%documents_fts%'
        """)
        rows = await cursor.fetchall()
        for row in rows:
            trigger_name = row[0]
            await self.db_connection.execute(f'DROP TRIGGER IF EXISTS "{trigger_name}"')
            logger.warning(f"已清理旧 Memory FTS 触发器: {trigger_name}")

    async def _count_documents(
        self, metadata_filters: dict[str, Any] | None = None
    ) -> int:
        filters = metadata_filters or {}
        if self.faiss_db is not None:
            return await self.faiss_db.document_storage.count_documents(
                metadata_filters=filters
            )
        if self.db_connection is None:
            return 0
        cursor = await self.db_connection.execute("SELECT metadata FROM documents")
        rows = await cursor.fetchall()
        return sum(
            1
            for row in rows
            if all(
                self._safe_json_dict(row[0]).get(key) == value
                for key, value in filters.items()
            )
        )

    async def _get_documents(
        self,
        *,
        metadata_filters: dict[str, Any] | None = None,
        ids: list[int] | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        filters = metadata_filters or {}
        if self.faiss_db is not None:
            return await self.faiss_db.document_storage.get_documents(
                metadata_filters=filters,
                ids=ids,
                limit=limit,
                offset=offset,
            )
        if self.db_connection is None:
            return []
        params: list[Any] = []
        conditions: list[str] = []
        if ids:
            placeholders = ",".join("?" for _ in ids)
            conditions.append(f"id IN ({placeholders})")
            params.extend(int(item) for item in ids)
        for key, value in filters.items():
            conditions.append("json_extract(metadata, ?) = ?")
            params.extend((f"$.{key}", value))
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.extend((max(1, int(limit)), max(0, int(offset))))
        cursor = await self.db_connection.execute(
            f"""
            SELECT id, doc_id, text, metadata, created_at, updated_at
            FROM documents
            {where}
            ORDER BY id ASC
            LIMIT ? OFFSET ?
            """,
            params,
        )
        rows = await cursor.fetchall()
        documents: list[dict[str, Any]] = []
        for row in rows:
            metadata = self._safe_json_dict(row["metadata"])
            if any(metadata.get(key) != value for key, value in filters.items()):
                continue
            documents.append(
                {
                    "id": int(row["id"]),
                    "doc_id": row["doc_id"],
                    "text": row["text"],
                    "metadata": metadata,
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
            )
        return documents

    async def _projection_write_status(self, memory_id: int) -> str | None:
        if self.db_connection is None:
            return None
        cursor = await self.db_connection.execute(
            """
            SELECT status
            FROM memory_write_ops
            WHERE op_type = 'add' AND memory_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (memory_id,),
        )
        row = await cursor.fetchone()
        return str(row[0]) if row else None

    async def _mark_projection_stale(self, document_id: int) -> None:
        memory = await self.get_memory(document_id)
        if memory is None:
            return
        metadata = self._safe_json_dict(memory.get("metadata"))
        if metadata.get("archive_type") != "memory_item_projection":
            return
        if self.hybrid_retriever is None or self.bm25_retriever is None:
            raise RuntimeError("文档检索器未初始化")

        updated = await self.hybrid_retriever.update_metadata(
            document_id,
            {"projection_status": "stale", "protected": False},
        )
        if not updated:
            raise RuntimeError(f"旧对象投影元数据更新失败: {document_id}")
        if not await self.bm25_retriever.delete_document(document_id):
            raise RuntimeError(f"旧对象投影 FTS 移除失败: {document_id}")

        if self.graph_store is not None:
            vector_doc_ids = await self.graph_store.mark_memory_projection_status(
                document_id, "stale"
            )
            if self.graph_vector_retriever is not None:
                for vector_doc_id in vector_doc_ids:
                    updated = await self.graph_vector_retriever.update_metadata(
                        vector_doc_id, {"projection_status": "stale"}
                    )
                    if not updated:
                        raise RuntimeError(
                            f"旧对象图向量投影更新失败: {vector_doc_id}"
                        )
        if self.atom_store is not None:
            await self.atom_store.mark_projection_stale_by_parent(document_id)

    async def project_evolving_memory_mutation(
        self, result: MutationResult
    ) -> dict[str, int]:
        """Create document/vector/graph/atom projections for one canonical revision."""
        item = result.item
        if (
            result.deduplicated
            and item.index_status.value == "current"
            and item.current_document_id is not None
        ):
            return {"current_document_id": int(item.current_document_id)}

        old_projection_ids: set[int] = set()
        if self.evolving_memory_store is not None:
            for item_id in result.affected_item_ids:
                affected = await self.evolving_memory_store.get_item(
                    owner_user_id=item.owner_user_id,
                    memory_item_id=item_id,
                )
                if affected is not None and affected.current_document_id is not None:
                    old_projection_ids.add(int(affected.current_document_id))
        if item.current_document_id is not None:
            old_projection_ids.add(int(item.current_document_id))

        metadata = {
            "archive_type": "memory_item_projection",
            "protected": True,
            "projection_status": "current",
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
            "version": item.version,
            "canonical_summary": item.content,
            "key_facts": [item.content],
        }
        atom = MemoryAtom(
            parent_memory_id=0,
            atom_type=AtomType.FACTUAL,
            content=item.content,
            importance=float(item.importance),
            confidence=float(item.confidence),
            session_id=item.session_id,
            persona_id=item.persona_id,
            metadata=dict(metadata),
            memory_item_id=item.memory_item_id,
            memory_revision_no=item.current_revision_no,
        )

        new_document_id: int | None = None
        try:
            new_document_id = await self.add_memory(
                item.content,
                session_id=item.session_id,
                persona_id=item.persona_id,
                importance=float(item.importance),
                metadata=metadata,
                atoms=[atom],
            )
            write_status = await self._projection_write_status(new_document_id)
            if write_status == "needs_repair":
                raise RuntimeError("对象投影的图或原子索引未完整写入")

            for document_id in sorted(old_projection_ids):
                if document_id != new_document_id:
                    await self._mark_projection_stale(document_id)

            if item.status not in {
                MemoryItemStatus.ACTIVE,
                MemoryItemStatus.CONFLICTED,
            } and self.atom_store is not None:
                await self.atom_store.mark_projection_stale_by_parent(new_document_id)
            return {"current_document_id": new_document_id}
        except asyncio.CancelledError:
            raise
        except Exception:
            if new_document_id is not None:
                try:
                    await self._mark_projection_stale(new_document_id)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning(
                        "[MemoryEngine] 失败的新对象投影无法完整标 stale",
                        exc_info=True,
                    )
            raise
        finally:
            self._evolving_generation += 1
            self._invalidate_search_cache()

    async def _safe_retrieval_route(self, route_name: str, coroutine):
        try:
            return await coroutine
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                f"[MemoryEngine] {route_name}失败，保留其他召回路",
                exc_info=True,
            )
            return []

    @staticmethod
    def _merge_evolving_results(
        item_results: list[HybridResult],
        legacy_results: list[HybridResult],
        k: int,
    ) -> list[HybridResult]:
        item_by_id = {
            result.memory_item_id: result
            for result in item_results
            if result.memory_item_id
        }
        item_projection_ids = {
            result.doc_id for result in item_results if result.doc_id > 0
        }
        merged = list(item_results)
        for result in legacy_results:
            metadata = result.metadata if isinstance(result.metadata, dict) else {}
            memory_item_id = result.memory_item_id or metadata.get("memory_item_id")
            if memory_item_id and memory_item_id in item_by_id:
                item_result = item_by_id[memory_item_id]
                item_result.final_score = min(
                    1.0, max(item_result.final_score, result.final_score) + 0.05
                )
                breakdown = item_result.score_breakdown or {}
                breakdown["projection_route_score"] = round(result.final_score, 4)
                item_result.score_breakdown = breakdown
                continue
            if result.doc_id in item_projection_ids:
                continue
            if memory_item_id:
                result.memory_item_id = str(memory_item_id)
            merged.append(result)
        merged.sort(key=lambda value: value.final_score, reverse=True)
        return merged[:k]

    # ==================== 核心记忆操作 ====================

    async def add_memory(
        self,
        content: str,
        session_id: str | None = None,
        persona_id: str | None = None,
        importance: float = 0.5,
        metadata: dict[str, Any] | None = None,
        atoms: list | None = None,
    ) -> int:
        """
        添加新记忆

        Args:
            content: 记忆内容
            session_id: 会话ID(支持多种格式,自动提取UUID)
            persona_id: 人格ID(支持多种格式,自动提取UUID)
            importance: 重要性(0-1)
            metadata: 额外元数据

        Returns:
            int: 记忆ID(doc_id)
        """
        if not content or not content.strip():
            raise ValueError("记忆内容不能为空")

        op_id = await self._start_write_op(
            "add",
            {
                "content_preview": content[:500],
                "session_id": session_id,
                "persona_id": persona_id,
                "importance": importance,
                "metadata": metadata or {},
                "atoms": [
                    self._serialize_atom_for_repair(atom) for atom in (atoms or [])
                ],
            },
        )

        # 准备完整元数据 - 保存完整的 unified_msg_origin，不提取UUID
        # 只在查询/过滤时才提取UUID进行匹配，存储时保留完整信息
        current_time = time.time()
        full_metadata = {
            "session_id": session_id,  # 保存完整的 unified_msg_origin
            "persona_id": persona_id,  # 保存完整的 persona_id
            "importance": max(0.0, min(1.0, importance)),  # 限制在0-1范围
            "create_time": current_time,
            "last_access_time": current_time,
        }

        # 合并用户提供的额外元数据
        # 注意：先合并外部metadata，再确保时间字段不被覆盖
        if metadata:
            full_metadata.update(metadata)

        # 确保时间字段始终存在且不被外部metadata覆盖
        full_metadata["create_time"] = current_time
        full_metadata["last_access_time"] = current_time

        # 通过混合检索器添加(会同时添加到BM25和向量索引)
        if self.hybrid_retriever is None:
            raise RuntimeError("混合检索器未初始化")
        try:
            doc_id = await self.hybrid_retriever.add_memory(content, full_metadata)
            await self._advance_write_op(
                op_id,
                "document_indexed",
                memory_id=doc_id,
                payload_patch={"memory_id": doc_id},
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            await self._advance_write_op(
                op_id,
                "document_failed",
                status="failed",
                error=str(e),
            )
            raise

        # 写入记忆原子
        atom_write_failed = False
        if atoms and self.atom_store is not None and self.atom_enabled:
            prepared_atoms = []
            for atom in atoms:
                atom.session_id = atom.session_id or session_id
                atom.persona_id = atom.persona_id or persona_id
                atom.parent_memory_id = doc_id
                prepared_atoms.append(atom)
            try:
                await self.atom_store.insert_many(prepared_atoms)
                await self._advance_write_op(
                    op_id,
                    "atoms_indexed",
                    memory_id=doc_id,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error("[MemoryEngine] 批量写入记忆原子失败", exc_info=True)
                failed_atoms: list[dict[str, Any]] = []
                for atom in prepared_atoms:
                    if getattr(atom, "atom_id", 0):
                        continue
                    try:
                        await self.atom_store.insert(atom)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        failed_atoms.append(self._serialize_atom_for_repair(atom))
                        logger.error(
                            f"[MemoryEngine] 写入记忆原子失败: {atom.content[:80]}",
                            exc_info=True,
                        )
                if failed_atoms:
                    await self._advance_write_op(
                        op_id,
                        "atoms_partial",
                        status="needs_repair",
                        memory_id=doc_id,
                        error="atom insert failed",
                        payload_patch={"failed_atoms": failed_atoms},
                    )
                    atom_write_failed = True
                else:
                    await self._advance_write_op(
                        op_id,
                        "atoms_indexed",
                        memory_id=doc_id,
                    )
        else:
            await self._advance_write_op(op_id, "atoms_skipped", memory_id=doc_id)

        needs_repair = atom_write_failed
        if self.graph_memory_manager is not None:
            try:
                await self.graph_memory_manager.index_memory(
                    doc_id, content, full_metadata, atoms
                )
                await self._advance_write_op(
                    op_id,
                    "graph_indexed",
                    status="needs_repair" if needs_repair else "pending",
                    memory_id=doc_id,
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                await self._advance_write_op(
                    op_id,
                    "graph_failed",
                    status="needs_repair",
                    memory_id=doc_id,
                    error=str(e),
                )
                needs_repair = True
                logger.error(
                    f"[MemoryEngine] 图记忆索引失败，已标记待修复 (memory_id={doc_id})",
                    exc_info=True,
                )
        else:
            await self._advance_write_op(
                op_id,
                "graph_skipped",
                status="needs_repair" if needs_repair else "pending",
                memory_id=doc_id,
            )

        if not needs_repair:
            await self._advance_write_op(
                op_id,
                "completed",
                status="completed",
                memory_id=doc_id,
            )
        self._invalidate_search_cache()
        return doc_id

    async def search_memories(
        self,
        query: str,
        k: int = 5,
        session_id: str | None = None,
        persona_id: str | None = None,
        access_context: MemoryAccessContext | None = None,
    ) -> list[HybridResult]:
        """Search item, legacy document and graph routes with per-route isolation."""
        if not query or not query.strip():
            return []

        effective_session_id = session_id or (
            access_context.session_id if access_context is not None else None
        )
        effective_persona_id = persona_id or (
            access_context.persona_id if access_context is not None else None
        )
        object_generation = None
        if access_context is not None and self.evolving_memory_store is not None:
            object_generation = await self.evolving_memory_store.get_generation_token(
                access_context.owner_user_id
            )
        cache_key = self._search_cache_key(
            query,
            k,
            effective_session_id,
            effective_persona_id,
            access_context,
            object_generation,
        )
        cached_results = self._get_cached_search_results(cache_key)
        if cached_results is not None:
            for result in cached_results:
                if result.source_type.startswith("legacy") and result.doc_id > 0:
                    self._create_tracked_task(
                        self._update_access_time_internal(result.doc_id)
                    )
            return cached_results

        if effective_session_id and ":" in effective_session_id:
            self._create_tracked_task(
                self._migrate_session_data_if_needed(effective_session_id)
            )

        if self.hybrid_retriever is None:
            raise RuntimeError("混合检索器未初始化")
        legacy_coroutine = (
            self.dual_route_retriever.search(
                query,
                max(k * 2, k),
                effective_session_id,
                effective_persona_id,
                access_context=access_context,
            )
            if self.dual_route_retriever is not None
            else self.hybrid_retriever.search(
                query,
                max(k * 2, k),
                effective_session_id,
                effective_persona_id,
                access_context=access_context,
            )
        )
        item_enabled = bool(
            access_context is not None
            and self.evolving_memory_retriever is not None
            and self.evolving_memory_manager is not None
            and self.evolving_memory_manager.evolving_config.get("enabled", True)
            and self.evolving_memory_manager.evolving_config.get("read_enabled", True)
        )
        item_coroutine = (
            self.evolving_memory_retriever.search(
                query, max(k * 2, k), access_context
            )
            if item_enabled
            else asyncio.sleep(0, result=[])
        )
        item_results, legacy_results = await asyncio.gather(
            self._safe_retrieval_route("对象路", item_coroutine),
            self._safe_retrieval_route("文档/图路", legacy_coroutine),
        )
        results = self._merge_evolving_results(item_results, legacy_results, k)

        for result in results:
            if result.source_type.startswith("legacy") and result.doc_id > 0:
                self._create_tracked_task(
                    self._update_access_time_internal(result.doc_id)
                )

        self._set_cached_search_results(cache_key, results)
        return results

    async def get_memory(self, memory_id: int) -> dict[str, Any] | None:
        """
        根据ID获取记忆

        Args:
            memory_id: 记忆ID

        Returns:
            Optional[Dict]: 记忆数据,包含text和metadata
        """
        try:
            docs = await self._get_documents(
                metadata_filters={}, ids=[memory_id], limit=1
            )

            if not docs or len(docs) == 0:
                return None

            doc = docs[0]
            return {
                "id": doc["id"],
                "text": doc["text"],
                "metadata": doc["metadata"],
            }
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("[MemoryEngine] 获取记忆详情失败", exc_info=True)
            return None

    async def update_memory(
        self,
        memory_id: int,
        updates: dict[str, Any],
    ) -> bool:
        """
        更新记忆（确保多数据库同步）

        支持更新内容、重要性、元数据等。采用不同策略：
        - 内容更新：先创建后删除（避免数据丢失）+ 全库同步
        - 元数据更新：三库同步更新

        Args:
            memory_id: 记忆ID
            updates: 更新字典,可包含:
                - content: 新内容 (触发完整重建)
                - importance: 新重要性
                - metadata: 元数据更新

        Returns:
            bool: 是否更新成功
        """
        # 获取当前记忆
        memory = await self.get_memory(memory_id)
        if not memory:
            logger.error(f"[更新] 记忆不存在 (memory_id={memory_id})")
            return False

        # 解析 metadata（可能是JSON字符串）
        current_metadata = memory.get("metadata", {})
        if isinstance(current_metadata, str):
            import json

            try:
                current_metadata = json.loads(current_metadata)
            except (json.JSONDecodeError, TypeError):
                current_metadata = {}
        elif not isinstance(current_metadata, dict):
            current_metadata = {}

        # 处理内容更新 (需要重建所有索引)
        if "content" in updates:
            new_content = updates["content"]
            if not new_content or not new_content.strip():
                return False

            try:
                # 保留必要信息
                session_id = current_metadata.get("session_id")
                persona_id = current_metadata.get("persona_id")
                importance = clamp_float(
                    current_metadata.get("importance", updates.get("importance", 0.5)),
                    default=0.5,
                )

                # 构建新元数据
                new_metadata = current_metadata.copy()
                new_metadata["updated_at"] = time.time()
                new_metadata["previous_id"] = memory_id  # 记录旧ID

                # 【改进】先创建新记忆，再删除旧记忆（避免数据丢失）
                logger.info(f"[更新] 开始内容更新流程 (old_id={memory_id})")

                # 1. 创建新记忆（自动在所有数据库创建）
                new_memory_id = await self.add_memory(
                    content=new_content,
                    session_id=session_id,
                    persona_id=persona_id,
                    importance=importance,
                    metadata=new_metadata,
                )

                if new_memory_id is None:
                    logger.error(f"[更新] 创建新记忆失败 (old_id={memory_id})")
                    return False

                logger.info(f"[更新] 新记忆已创建 (new_id={new_memory_id})")

                # 2. 删除旧记忆（从所有数据库删除）
                delete_success = await self.delete_memory(memory_id)
                if not delete_success:
                    # 旧记忆删除失败，回滚：删除刚创建的新记忆，避免重复记录
                    logger.warning(
                        f"[更新] 删除旧记忆失败，回滚新记忆 (old_id={memory_id}, new_id={new_memory_id})"
                    )
                    await self.delete_memory(new_memory_id)
                    return False

                logger.info(
                    f"[更新] 内容更新完成 (old_id={memory_id} → new_id={new_memory_id})"
                )
                self._invalidate_search_cache()
                return True

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(
                    f"[更新] 内容更新失败 (memory_id={memory_id}): {e}", exc_info=True
                )
                return False

        # 处理非内容的元数据更新（不需要重建索引）
        metadata_updates = {}

        if "importance" in updates:
            metadata_updates["importance"] = clamp_float(
                updates["importance"], default=0.5
            )

        if "metadata" in updates:
            metadata_updates.update(updates["metadata"])

        if metadata_updates:
            # 确保 current_metadata 是字典（再次检查）
            if not isinstance(current_metadata, dict):
                import json

                try:
                    current_metadata = (
                        json.loads(current_metadata)
                        if isinstance(current_metadata, str)
                        else {}
                    )
                except (json.JSONDecodeError, TypeError):
                    current_metadata = {}

            # 合并元数据
            current_metadata.update(metadata_updates)
            current_metadata["updated_at"] = time.time()

            # 【改进】使用增强的update_metadata确保三库同步
            if self.hybrid_retriever is None:
                logger.error("混合检索器未初始化")
                return False
            success = await self.hybrid_retriever.update_metadata(
                memory_id, metadata_updates
            )

            if success:
                logger.info(f"[更新] 元数据更新成功 (memory_id={memory_id})")
                if self.graph_memory_manager is not None:
                    await self.graph_memory_manager.index_memory(
                        memory_id,
                        memory["text"],
                        current_metadata,
                    )
                self._invalidate_search_cache()
            else:
                logger.error(f"[更新] 元数据更新失败 (memory_id={memory_id})")

            return success

        return True

    async def delete_memory(self, memory_id: int) -> bool:
        """
        删除记忆

        Args:
            memory_id: 记忆ID

        Returns:
            bool: 是否删除成功
        """

        op_id = await self._start_write_op(
            "delete",
            {"memory_id": memory_id},
            memory_id=memory_id,
        )

        # hybrid_retriever.delete_memory() 内部已按顺序删除 BM25、向量索引和 documents 表
        if self.hybrid_retriever is None:
            logger.error("混合检索器未初始化")
            await self._advance_write_op(
                op_id,
                "document_delete_failed",
                status="failed",
                error="hybrid retriever not initialized",
            )
            return False
        success = await self.hybrid_retriever.delete_memory(memory_id)
        if not success:
            await self._advance_write_op(
                op_id,
                "document_delete_failed",
                status="failed",
                error="document/vector delete failed",
            )
            return False

        await self._advance_write_op(op_id, "document_deleted", memory_id=memory_id)

        needs_repair = False
        try:
            if self.graph_memory_manager is not None:
                await self.graph_memory_manager.delete_memory(memory_id)
            await self._advance_write_op(op_id, "graph_deleted", memory_id=memory_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            await self._advance_write_op(
                op_id,
                "graph_delete_failed",
                status="needs_repair",
                memory_id=memory_id,
                error=str(e),
            )
            needs_repair = True
            logger.error(
                f"[MemoryEngine] 图记忆删除失败，已标记待修复 (memory_id={memory_id})",
                exc_info=True,
            )

        try:
            if self.atom_store is not None:
                await self.atom_store.delete_by_parent(memory_id)
            await self._advance_write_op(op_id, "atoms_deleted", memory_id=memory_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            await self._advance_write_op(
                op_id,
                "atom_delete_failed",
                status="needs_repair",
                memory_id=memory_id,
                error=str(e),
            )
            needs_repair = True
            logger.error(
                f"[MemoryEngine] 记忆原子删除失败，已标记待修复 (memory_id={memory_id})",
                exc_info=True,
            )

        if not needs_repair:
            await self._advance_write_op(
                op_id,
                "completed",
                status="completed",
                memory_id=memory_id,
            )
        self._invalidate_search_cache()
        return success

    async def rebuild_graph_index(self) -> dict[str, int]:
        """Rebuild graph-memory artifacts from stored documents."""
        if self.graph_memory_manager is None:
            return {"rebuilt": 0, "skipped": 0}

        total_count = await self._count_documents(metadata_filters={})
        batch_size = 200
        offset = 0
        rebuilt = 0
        skipped = 0

        while offset < total_count:
            docs = await self._get_documents(
                metadata_filters={},
                limit=batch_size,
                offset=offset,
            )
            if not docs:
                break

            for doc in docs:
                metadata = doc.get("metadata") or {}
                if isinstance(metadata, str):
                    try:
                        metadata = json.loads(metadata)
                    except (json.JSONDecodeError, TypeError):
                        metadata = {}
                elif not isinstance(metadata, dict):
                    metadata = {}
                content = str(doc.get("text") or "")
                if not content.strip():
                    skipped += 1
                    continue
                await self.graph_memory_manager.index_memory(
                    doc["id"], content, metadata
                )
                rebuilt += 1

            offset += batch_size

        self._invalidate_search_cache()
        return {"rebuilt": rebuilt, "skipped": skipped}

    # ==================== 高级功能 ====================

    async def update_importance(self, memory_id: int, new_importance: float) -> bool:
        """
        更新记忆重要性

        Args:
            memory_id: 记忆ID
            new_importance: 新重要性值(0-1)

        Returns:
            bool: 是否更新成功
        """
        return await self.update_memory(memory_id, {"importance": new_importance})

    async def apply_daily_decay(self, decay_rate: float, days: int = 1) -> int:
        """
        批量应用重要性衰减

        Args:
            decay_rate: 每日衰减率 (0-1)
            days: 衰减天数（用于补偿错过的天数）

        Returns:
            int: 受影响的记忆数量
        """
        if decay_rate <= 0 or days <= 0:
            return 0

        if self.db_connection is None:
            logger.error("[衰减] 数据库连接未初始化")
            return 0

        try:
            if decay_rate >= 1:
                decay_rate = 1.0
            access_window_days = float(
                self.config.get("access_decay_window_days", 30.0)
            )
            max_access_count = float(self.config.get("access_decay_max_count", 10.0))
            access_decay_multiplier = float(
                self.config.get("access_count_decay_multiplier", 0.5)
            )
            access_window_start = time.time() - max(1.0, access_window_days) * 86400.0
            access_decay_multiplier = max(0.0, min(1.0, access_decay_multiplier))
            cursor = await self.db_connection.execute(
                "SELECT id, metadata FROM documents WHERE json_extract(metadata, '$.importance') IS NOT NULL OR metadata LIKE '%\"importance\"%'"
            )
            rows = await cursor.fetchall()
            updates: list[tuple[str, int]] = []

            for row in rows:
                metadata = self._safe_json_dict(row["metadata"])
                importance = clamp_float(metadata.get("importance"), default=0.5)
                access_count = safe_float(metadata.get("access_count"), 0.0)
                last_access_time = safe_float(metadata.get("last_access_time"), 0.0)

                recent_access_factor = (
                    1.0 if last_access_time >= access_window_start else 0.5
                )
                access_factor = min(1.0, access_count / max(1.0, max_access_count))
                effective_decay_rate = decay_rate * (
                    1 - 0.5 * access_factor * recent_access_factor
                )
                decay_factor = (1 - effective_decay_rate) ** days
                metadata["importance"] = max(
                    0.01,
                    round(importance * decay_factor, 4),
                )
                metadata["access_count"] = int(access_count * access_decay_multiplier)
                updates.append(
                    (json.dumps(metadata, ensure_ascii=False), int(row["id"]))
                )

            if not updates:
                return 0

            await self.db_connection.executemany(
                "UPDATE documents SET metadata = ? WHERE id = ?",
                updates,
            )

            await self.db_connection.commit()
            affected = len(updates)

            logger.info(
                f"[衰减] 批量衰减完成: 衰减率={decay_rate}, 天数={days}, "
                f"访问窗口={access_window_days:.1f}天, 影响记录={affected}"
            )

            self._invalidate_search_cache()
            return affected

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[衰减] 批量衰减失败: {e}", exc_info=True)
            return 0

    async def update_access_time(self, memory_id: int) -> bool:
        """
        更新最后访问时间

        Args:
            memory_id: 记忆ID

        Returns:
            bool: 是否更新成功
        """
        return await self._update_access_time_internal(memory_id)

    async def _update_access_time_internal(self, memory_id: int) -> bool:
        """内部方法:更新访问时间（直接更新documents表，不经过FAISS）"""
        import json

        current_time = time.time()

        try:
            if self.db_connection is None:
                return False

            # 直接更新 documents 表，不经过 FAISS
            # 1. 获取当前 metadata
            cursor = await self.db_connection.execute(
                "SELECT metadata FROM documents WHERE id = ?", (memory_id,)
            )
            row = await cursor.fetchone()

            if not row:
                return False

            # 2. 解析并更新 metadata
            metadata_str = row[0] if row and row[0] else "{}"
            try:
                metadata = (
                    json.loads(metadata_str)
                    if isinstance(metadata_str, str)
                    else metadata_str
                )
                if not isinstance(metadata, dict):
                    metadata = {}
            except (json.JSONDecodeError, TypeError):
                metadata = {}

            metadata["last_access_time"] = current_time
            try:
                access_count = int(metadata.get("access_count", 0) or 0)
            except (TypeError, ValueError):
                access_count = 0
            metadata["access_count"] = min(access_count + 1, 1_000_000)

            # 3. 写回 documents 表
            await self.db_connection.execute(
                "UPDATE documents SET metadata = ? WHERE id = ?",
                (json.dumps(metadata, ensure_ascii=False), memory_id),
            )
            await self.db_connection.commit()

            return True

        except asyncio.CancelledError:
            raise
        except Exception as e:
            # 记录错误但不影响查询流程
            logger.warning(
                f"更新访问时间失败 (memory_id={memory_id}): {e}",
                exc_info=True,
            )
            return False

    async def get_session_memories(
        self,
        session_id: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """
        获取会话的所有记忆（使用分批处理和数据库排序优化）

        Args:
            session_id: 会话ID(应传入完整的unified_msg_origin格式)
            limit: 限制数量

        Returns:
            List[Dict]: 记忆列表
        """
        # 【关键修改】不再提取UUID，直接使用完整的session_id进行匹配
        # 因为现在数据库中存储的就是完整的unified_msg_origin格式

        # 使用数据库层面的排序和分页，避免加载所有数据
        try:
            # 先获取总数判断是否需要分批
            total_count = await self._count_documents(
                metadata_filters={"session_id": session_id}
            )

            if total_count == 0:
                return []

            # 如果总数小于等于limit，直接一次性获取
            if total_count <= limit:
                all_docs = await self._get_documents(
                    metadata_filters={"session_id": session_id},
                    limit=limit,
                    offset=0,
                )
                # 通过线程池批量规范化 metadata（避免大量 json.loads 阻塞事件循环）
                all_docs = await asyncio.to_thread(
                    self._normalize_batch_metadata, all_docs
                )
                sorted_docs = sorted(
                    all_docs,
                    key=lambda d: safe_float(
                        d.get("metadata", {}).get("create_time"), 0.0
                    ),
                    reverse=True,
                )
            else:
                all_docs = []
                batch_size = 500
                offset = 0

                while offset < total_count:
                    batch = await self._get_documents(
                        metadata_filters={"session_id": session_id},
                        limit=batch_size,
                        offset=offset,
                    )

                    if not batch:
                        break

                    batch = await asyncio.to_thread(
                        self._normalize_batch_metadata, batch
                    )
                    all_docs.extend(batch)
                    offset += batch_size

                sorted_docs = sorted(
                    all_docs,
                    key=lambda d: safe_float(
                        d.get("metadata", {}).get("create_time"), 0.0
                    ),
                    reverse=True,
                )[:limit]

            memories = []
            for doc in sorted_docs:
                memories.append(
                    {
                        "id": doc["id"],
                        "text": doc["text"],
                        "metadata": doc["metadata"],
                    }
                )

            return memories
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                f"[MemoryEngine] 获取会话记忆失败 (session_id={session_id})",
                exc_info=True,
            )
            return []

    async def batch_delete_memories(self, memory_ids: list[int]) -> int:
        """Batch delete multiple memories using bulk SQL operations."""
        if not memory_ids:
            return 0

        if self.db_connection is None:
            logger.error("[批量删除] 数据库连接未初始化")
            return 0

        self._invalidate_search_cache()
        total_deleted = 0
        sql_batch_size = 200

        for i in range(0, len(memory_ids), sql_batch_size):
            batch = memory_ids[i : i + sql_batch_size]
            placeholders = ",".join("?" * len(batch))
            op_id = await self._start_write_op(
                "batch_delete",
                {
                    "memory_ids": batch,
                    "batch_offset": i,
                    "batch_size": len(batch),
                },
            )
            batch_deleted = 0

            try:
                # 1. Batch delete from BM25 FTS
                await self.db_connection.execute(
                    f"DELETE FROM livingmemory_memories_fts WHERE doc_id IN ({placeholders})",
                    batch,
                )
                await self._advance_write_op(
                    op_id,
                    "bm25_deleted",
                    payload_patch={"memory_ids": batch},
                )

                # 2. Look up UUIDs and delete from FAISS vector DB
                cursor = await self.db_connection.execute(
                    f"SELECT id, doc_id FROM documents WHERE id IN ({placeholders})",
                    batch,
                )
                uuid_rows = await cursor.fetchall()
                found_ids = [int(row["id"]) for row in uuid_rows]
                for row in uuid_rows:
                    uuid_doc_id = row["doc_id"]
                    if uuid_doc_id and self.faiss_db is not None:
                        try:
                            await self.faiss_db.delete(uuid_doc_id)
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            logger.warning(
                                f"[批量删除] FAISS 删除失败 (id={row['id']})",
                                exc_info=True,
                            )
                await self._advance_write_op(
                    op_id,
                    "faiss_deleted",
                    payload_patch={"memory_ids": batch, "found_ids": found_ids},
                )

                # 3. Batch delete from documents table
                cursor = await self.db_connection.execute(
                    f"DELETE FROM documents WHERE id IN ({placeholders})",
                    batch,
                )
                await self.db_connection.commit()
                batch_deleted = int(cursor.rowcount or 0)
                await self._advance_write_op(
                    op_id,
                    "documents_deleted",
                    payload_patch={
                        "memory_ids": batch,
                        "found_ids": found_ids,
                        "deleted_count": batch_deleted,
                    },
                )

                # 4. Batch delete graph artifacts and atoms
                await self._delete_graph_and_atoms_for_batch(batch)
                await self._advance_write_op(
                    op_id,
                    "graph_atoms_deleted",
                    payload_patch={"memory_ids": batch, "deleted_count": batch_deleted},
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                await self._advance_write_op(
                    op_id,
                    "batch_delete_failed",
                    status="needs_repair",
                    error=str(e),
                    payload_patch={
                        "memory_ids": batch,
                        "deleted_count": batch_deleted,
                    },
                )
                logger.error(
                    f"[批量删除] 批次删除失败 (offset={i}, size={len(batch)})",
                    exc_info=True,
                )
                raise

            await self._advance_write_op(
                op_id,
                "completed",
                status="completed",
                payload_patch={"memory_ids": batch, "deleted_count": batch_deleted},
            )
            total_deleted += batch_deleted

        if total_deleted:
            logger.info(f"[批量删除] 共删除 {total_deleted} 条记忆")
        return total_deleted

    async def cleanup_old_memories(
        self,
        days_threshold: int | None = None,
        importance_threshold: float | None = None,
    ) -> int:
        """
        清理旧记忆（使用分批处理避免内存问题）

        删除超过阈值且重要性低的记忆

        Args:
            days_threshold: 天数阈值,默认从配置读取
            importance_threshold: 重要性阈值,默认从配置读取

        Returns:
            int: 删除的记忆数量
        """
        # 使用配置或参数值
        days = (
            self.config.get("cleanup_days_threshold", 30)
            if days_threshold is None
            else days_threshold
        )
        importance = (
            self.config.get("cleanup_importance_threshold", 0.3)
            if importance_threshold is None
            else importance_threshold
        )
        try:
            days = int(days)
            importance = float(importance)
        except (TypeError, ValueError):
            logger.error(
                f"清理参数格式错误: days_threshold={days}, importance_threshold={importance}"
            )
            return 0

        if days < 0:
            logger.error(f"清理参数无效: days_threshold={days}（必须 >= 0）")
            return 0

        cutoff_time = time.time() - (days * 86400)

        # 分批扫描文档并删除，避免一次性加载所有数据到内存
        try:
            # 先获取总数
            total_count = await self._count_documents(metadata_filters={})

            if total_count == 0:
                return 0

            batch_size = 500
            offset = 0
            to_delete_ids: list[int] = []

            # First pass: scan candidates without deleting to avoid offset-shift skips.
            while offset < total_count:
                batch_docs = await self._get_documents(
                    metadata_filters={}, limit=batch_size, offset=offset
                )

                if not batch_docs:
                    break

                batch_docs = await asyncio.to_thread(
                    self._normalize_batch_metadata, batch_docs
                )

                for doc in batch_docs:
                    metadata = doc["metadata"]
                    if bool(metadata.get("protected", False)) or (
                        metadata.get("archive_type") == "memory_item_projection"
                        and metadata.get("projection_status", "current") == "current"
                    ):
                        continue

                    create_time = safe_float(metadata.get("create_time"), time.time())
                    doc_importance = clamp_float(
                        metadata.get("importance"), default=0.5
                    )

                    if create_time < cutoff_time and doc_importance < importance:
                        to_delete_ids.append(doc["id"])

                offset += len(batch_docs)
                if len(batch_docs) < batch_size:
                    break

            if not to_delete_ids:
                return 0

            logger.info(f"[清理] 发现 {len(to_delete_ids)} 条候选记忆，开始批量删除")
            deleted_count = await self.batch_delete_memories(to_delete_ids)
            logger.info(f"[清理] 完成，已删除 {deleted_count} 条旧记忆")

            return deleted_count
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("[清理] 清理旧记忆失败", exc_info=True)
            return 0

    async def _migrate_session_data_if_needed(self, unified_msg_origin: str) -> None:
        """
        运行时自动迁移：将旧格式的session_id更新为unified_msg_origin格式

        支持各种平台的旧格式（通用匹配策略）：
        - WebChat UUID: "ac8c2cef-959e-4146-ad22-c82d0230ad06"
        - WebChat带前缀: "webchat!astrbot!ac8c2cef-959e-4146-ad22-c82d0230ad06"
        - QQ号: "123456789"
        - 其他平台: 任意字符串

        目标格式: "platform:message_type:session_id"

        策略：
        1. 从unified_msg_origin解析出：platform、message_type、session_id
        2. 生成所有可能的旧格式匹配候选（递归拆分）
        3. 查找匹配任一候选且不含冒号的旧记录
        4. 批量更新为unified_msg_origin
        5. 使用unified_msg_origin本身作为迁移标记（避免重复）

        Args:
            unified_msg_origin: 完整的统一消息来源（格式：platform:type:session_id）
        """

        try:
            # 1. 解析 unified_msg_origin
            parts = unified_msg_origin.split(":", 2)
            if len(parts) != 3:
                logger.warning(
                    f"[自动迁移] unified_msg_origin 格式不正确: {unified_msg_origin}"
                )
                return

            platform_id, message_type, full_session_id = parts

            # 2. 生成所有可能的旧格式匹配候选
            # 对于 "webchat!astrbot!ac8c2cef-..." 会生成:
            #   ["webchat!astrbot!ac8c2cef-...", "astrbot!ac8c2cef-...", "ac8c2cef-..."]
            # 对于 "123456789" 会生成: ["123456789"]
            candidates = [full_session_id]

            # 按感叹号递归拆分
            if "!" in full_session_id:
                parts_by_bang = full_session_id.split("!")
                for i in range(1, len(parts_by_bang)):
                    candidates.append("!".join(parts_by_bang[i:]))

            logger.info(f"[自动迁移] 开始检查会话，候选匹配: {candidates}")

            # 3. 检查是否已迁移（使用unified_msg_origin本身作为标记）
            migration_key = f"migrated_umo_{unified_msg_origin}"
            if self.db_connection is None:
                return
            cursor = await self.db_connection.execute(
                "SELECT value FROM migration_status WHERE key = ?", (migration_key,)
            )
            row = await cursor.fetchone()
            if row and row[0] == "true":
                # 已迁移过，跳过
                return

            # 4. 查找所有需要迁移的记录
            # 条件：session_id 匹配任一候选 且 不包含冒号（旧格式标识）
            placeholders = " OR ".join(
                ["json_extract(metadata, '$.session_id') = ?" for _ in candidates]
            )
            query = f"""
                SELECT id, metadata FROM documents
                WHERE ({placeholders})
                AND json_extract(metadata, '$.session_id') NOT LIKE '%:%'
            """

            cursor = await self.db_connection.execute(query, tuple(candidates))
            rows = list(await cursor.fetchall())

            if not rows:
                logger.info("[自动迁移] 未找到需要迁移的旧数据")
                # 即使没有旧数据也标记为已检查，避免重复查询
                await self.db_connection.execute(
                    "INSERT OR REPLACE INTO migration_status (key, value, updated_at) VALUES (?, ?, datetime('now'))",
                    (migration_key, "true"),
                )
                await self.db_connection.commit()
                return

            logger.info(f"[自动迁移] 找到 {len(list(rows))} 条旧数据需要迁移")

            # 5. 批量更新
            updated_count = 0
            for row in rows:
                doc_id = row[0]
                metadata_str = row[1]

                try:
                    metadata = json.loads(metadata_str) if metadata_str else {}
                except (json.JSONDecodeError, TypeError):
                    metadata = {}

                old_session_id = metadata.get("session_id", "unknown")

                # 更新为unified_msg_origin格式
                metadata["session_id"] = unified_msg_origin
                metadata["migrated_at"] = time.time()
                metadata["old_session_id"] = old_session_id  # 保留旧值便于追溯

                # 写回数据库
                await self.db_connection.execute(
                    "UPDATE documents SET metadata = ? WHERE id = ?",
                    (json.dumps(metadata, ensure_ascii=False), doc_id),
                )
                updated_count += 1

            # 6. 提交更新
            await self.db_connection.commit()

            # 7. 标记为已迁移
            await self.db_connection.execute(
                "INSERT OR REPLACE INTO migration_status (key, value, updated_at) VALUES (?, ?, datetime('now'))",
                (migration_key, "true"),
            )
            await self.db_connection.commit()

            logger.info(
                f"[自动迁移] 完成！已更新 {updated_count} 条记录 -> {unified_msg_origin}"
            )

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[自动迁移] 迁移失败: {e}", exc_info=True)

    def get_runtime_status(self) -> dict[str, Any]:
        degraded_states: list[str] = []
        retrieval_mode = "full" if self.vector_retriever is not None else "bm25_only"
        llm_mode = "full" if self.llm_provider is not None else "no_llm"
        if retrieval_mode != "full":
            degraded_states.append(retrieval_mode)
        if llm_mode != "full":
            degraded_states.append(llm_mode)
        return {
            "status": "full" if not degraded_states else "degraded",
            "retrieval_mode": retrieval_mode,
            "llm_mode": llm_mode,
            "degraded_states": degraded_states,
            "evolving_memory_enabled": self.evolving_memory_retriever is not None,
            "graph_keyword_enabled": self.graph_keyword_retriever is not None,
            "graph_vector_enabled": self.graph_vector_retriever is not None,
        }

    async def get_statistics(self) -> dict[str, Any]:
        """
        获取记忆统计信息（使用批量处理避免内存问题）

        Returns:
            Dict: 统计信息,包含:
                - total_memories: 总记忆数
                - sessions: 各会话的记忆数（按UUID分组）
                - status_breakdown: 各状态的记忆数
                - avg_importance: 平均重要性
                - oldest_memory: 最旧记忆时间
                - newest_memory: 最新记忆时间
        """
        try:
            # 使用 count_documents() 高效获取总数（不加载数据）
            total_count = await self._count_documents(metadata_filters={})

            stats = {}
            stats["total_memories"] = total_count

            # 初始化统计变量
            session_counts: dict[str, int] = {}
            status_breakdown = {"active": 0, "archived": 0, "deleted": 0}
            importance_sum = 0
            importance_count = 0
            importance_distribution = {
                "0-1": 0, "1-2": 0, "2-3": 0, "3-4": 0, "4-5": 0,
                "5-6": 0, "6-7": 0, "7-8": 0, "8-9": 0, "9-10": 0,
            }
            oldest_time = None
            newest_time = None

            # 分批处理，每次加载500条，避免内存问题
            batch_size = 500
            offset = 0

            while offset < total_count:
                # 获取一批文档
                batch_docs = await self._get_documents(
                    metadata_filters={}, limit=batch_size, offset=offset
                )

                if not batch_docs:
                    break

                # 通过线程池批量规范化 metadata（避免大量 json.loads 阻塞事件循环）
                batch_docs = await asyncio.to_thread(
                    self._normalize_batch_metadata, batch_docs
                )

                for doc in batch_docs:
                    metadata = doc["metadata"]

                    # 统计会话（直接使用session_id分组）
                    session_id = metadata.get("session_id")
                    if session_id:
                        session_counts[session_id] = (
                            session_counts.get(session_id, 0) + 1
                        )

                    # 统计状态（默认 active）
                    status = metadata.get("status", "active")
                    if status in status_breakdown:
                        status_breakdown[status] += 1
                    else:
                        # 未知状态默认计入 active
                        status_breakdown["active"] += 1

                    # 统计重要性
                    importance = metadata.get("importance")
                    if importance is not None:
                        clamped = clamp_float(importance, default=0.5)
                        importance_sum += clamped
                        importance_count += 1
                        # 分桶统计 (0-10 归一化)
                        display_importance = clamped * 10 if clamped <= 1 else clamped
                        bucket_idx = min(9, max(0, int(display_importance)))
                        bucket_keys = [
                            "0-1", "1-2", "2-3", "3-4", "4-5",
                            "5-6", "6-7", "7-8", "8-9", "9-10",
                        ]
                        importance_distribution[bucket_keys[bucket_idx]] += 1

                    # 统计时间
                    create_time = metadata.get("create_time")
                    if create_time:
                        create_time = safe_float(create_time, 0.0)
                        if oldest_time is None or create_time < oldest_time:
                            oldest_time = create_time
                        if newest_time is None or create_time > newest_time:
                            newest_time = create_time

                # 移动到下一批
                offset += batch_size

            stats["sessions"] = session_counts
            stats["status_breakdown"] = status_breakdown
            stats["avg_importance"] = (
                importance_sum / importance_count if importance_count > 0 else 0.0
            )
            stats["importance_distribution"] = importance_distribution
            stats["oldest_memory"] = oldest_time
            stats["newest_memory"] = newest_time
            if self.graph_store is not None:
                stats.update(await self.graph_store.get_memory_entry_stats())
                stats["graph_memory_enabled"] = True
            else:
                stats["graph_memory_enabled"] = False

            stats["runtime"] = self.get_runtime_status()
            stats["evolving_memory_items"] = {}
            stats["evolving_memory_total"] = 0
            if self.db_connection is not None:
                cursor = await self.db_connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'memory_items'"
                )
                if await cursor.fetchone() is not None:
                    cursor = await self.db_connection.execute(
                        "SELECT status, COUNT(*) FROM memory_items GROUP BY status"
                    )
                    item_rows = await cursor.fetchall()
                    stats["evolving_memory_items"] = {
                        str(row[0]): int(row[1]) for row in item_rows
                    }
                    stats["evolving_memory_total"] = sum(
                        stats["evolving_memory_items"].values()
                    )
            return stats
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"获取统计信息失败: {e}", exc_info=True)
            return {
                "total_memories": 0,
                "sessions": {},
                "status_breakdown": {"active": 0, "archived": 0, "deleted": 0},
                "avg_importance": 0.0,
                "oldest_memory": None,
                "newest_memory": None,
                "graph_memory_enabled": bool(self.graph_store is not None),
                "runtime": self.get_runtime_status(),
                "evolving_memory_items": {},
                "evolving_memory_total": 0,
            }

    async def maintain_storage(self, *, vacuum: bool = False) -> dict[str, Any]:
        """Run SQLite storage maintenance and return size diagnostics."""
        try:
            db_path = Path(self.db_path)
            wal_path = Path(f"{self.db_path}-wal")
            before_size = db_path.stat().st_size if db_path.exists() else 0
            before_wal_size = wal_path.stat().st_size if wal_path.exists() else 0

            if self.db_connection is None:
                return {
                    "success": False,
                    "error": "database connection is not initialized",
                }

            for fts_table in (
                "livingmemory_memories_fts",
                "livingmemory_graph_entries_fts",
                "memory_atoms_fts",
            ):
                try:
                    await self.db_connection.execute(
                        f"INSERT INTO {fts_table}({fts_table}) VALUES ('optimize')"
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.debug(
                        f"[StorageMaintenance] 跳过 FTS optimize: {fts_table}",
                        exc_info=True,
                    )

            await self.db_connection.commit()
            await self.db_connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

            if vacuum:
                await self.db_connection.execute("VACUUM")

            after_size = db_path.stat().st_size if db_path.exists() else 0
            after_wal_size = wal_path.stat().st_size if wal_path.exists() else 0
            return {
                "success": True,
                "vacuum": vacuum,
                "db_size_before": before_size,
                "db_size_after": after_size,
                "wal_size_before": before_wal_size,
                "wal_size_after": after_wal_size,
                "bytes_reclaimed": max(
                    0,
                    before_size + before_wal_size - after_size - after_wal_size,
                ),
            }
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[StorageMaintenance] 执行存储维护失败: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    @staticmethod
    def _normalize_batch_metadata(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Normalize metadata from JSON strings to dicts for a batch of documents.

        Offloaded to thread pool in batch processing paths to avoid blocking
        the event loop with hundreds of json.loads calls.
        """
        for doc in docs:
            metadata = doc.get("metadata")
            if isinstance(metadata, str):
                try:
                    doc["metadata"] = json.loads(metadata)
                except (json.JSONDecodeError, TypeError):
                    doc["metadata"] = {}
            elif not isinstance(metadata, dict):
                doc["metadata"] = {}
        return docs
