"""
数据库迁移管理器 - 处理数据库版本升级和数据迁移
"""

import asyncio
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from astrbot.api import logger


class DBMigration:
    """数据库迁移管理器"""

    # 当前数据库版本
    CURRENT_VERSION = 9

    # 版本历史记录
    VERSION_HISTORY = {
        1: "初始版本 - 基础记忆存储",
        2: "FTS5索引预处理 - 添加分词和停用词支持",
        3: "会话ID迁移 - 标记需要session_id格式升级",
        4: "Schema v2 - 双通道总结字段 + source_window 溯源支持",
        5: "Graph memory - graph tables and dual-route retrieval metadata",
        6: "插件 FTS 表统一 livingmemory 前缀，旧 documents_fts 安全重命名备份",
        7: "Storage indexes and FTS optimization for graph and atom data",
        8: "Write-operation log and access-aware metadata indexes",
        9: "Owner-scoped evolving memory objects and immutable revisions",
    }

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.migration_lock = asyncio.Lock()

    async def get_db_version(self) -> int:
        """
        获取当前数据库版本

        Returns:
            int: 数据库版本号，如果不存在版本表则返回1（旧版本）
        """
        try:
            async with aiosqlite.connect(self.db_path) as db:
                # 检查版本表是否存在
                cursor = await db.execute("""
                    SELECT name FROM sqlite_master
                    WHERE type='table' AND name='db_version'
                """)
                table_exists = await cursor.fetchone()

                if not table_exists or len(table_exists) == 0:
                    # 没有版本表，检查是否有documents表（判断是否为旧数据库）
                    cursor = await db.execute("""
                        SELECT name FROM sqlite_master
                        WHERE type='table' AND name='documents'
                    """)
                    has_documents = await cursor.fetchone()

                    if has_documents:
                        # 有documents表但没有版本表，检查是否有数据
                        cursor = await db.execute("SELECT COUNT(*) FROM documents")
                        doc_count_row = await cursor.fetchone()
                        doc_count = doc_count_row[0] if doc_count_row else 0

                        if doc_count > 0:
                            # 有数据但无版本表，判定为v1旧数据库
                            # 注意：v2数据库在初始化时会自动创建版本表，不会出现这种情况
                            logger.info(
                                f"检测到旧版本数据库（无版本表，有{doc_count}条数据），当前版本: 1"
                            )
                            return 1
                        else:
                            # 空数据库，视为最新版本
                            logger.info(
                                "检测到空数据库（已初始化但无数据），视为最新版本"
                            )
                            return self.CURRENT_VERSION
                    else:
                        # 全新数据库，没有任何表，视为最新版本
                        logger.info("检测到全新数据库，视为最新版本")
                        return self.CURRENT_VERSION

                # 读取版本号
                cursor = await db.execute(
                    "SELECT version FROM db_version ORDER BY id DESC LIMIT 1"
                )
                row = await cursor.fetchone()

                if row and len(row) > 0:
                    version = row[0]
                    logger.info(f"当前数据库版本: {version}")
                    return version
                else:
                    return 1

        except Exception as e:
            logger.error(f"获取数据库版本失败: {e}", exc_info=True)
            return 1

    async def initialize_version_table(self):
        """初始化版本管理表"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS db_version (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        version INTEGER NOT NULL,
                        description TEXT,
                        migrated_at TEXT NOT NULL,
                        migration_duration_seconds REAL
                    )
                """)
                await db.commit()
                logger.info("数据库版本管理表初始化完成")
        except Exception as e:
            logger.error(f"初始化版本表失败: {e}", exc_info=True)
            raise

    async def set_db_version(
        self, version: int, description: str = "", duration: float = 0.0
    ):
        """
        设置数据库版本

        Args:
            version: 版本号
            description: 版本描述
            duration: 迁移耗时（秒）
        """
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    """
                    INSERT INTO db_version (version, description, migrated_at, migration_duration_seconds)
                    VALUES (?, ?, ?, ?)
                """,
                    (version, description, datetime.now(timezone.utc).isoformat(), duration),
                )
                await db.commit()
                logger.info(f"数据库版本已更新至: {version}")
        except Exception as e:
            logger.error(f"设置数据库版本失败: {e}", exc_info=True)
            raise

    async def needs_migration(self) -> bool:
        """
        检查是否需要迁移

        Returns:
            bool: True表示需要迁移
        """
        current_version = await self.get_db_version()
        needs_migration = current_version < self.CURRENT_VERSION

        if needs_migration:
            logger.warning(
                f"数据库需要迁移: v{current_version} -> v{self.CURRENT_VERSION}"
            )
        else:
            logger.info(f"数据库版本最新: v{current_version}")

        return needs_migration

    async def migrate(
        self,
        progress_callback: Callable[[str, int, int], None] | None = None,
    ) -> dict[str, Any]:
        """
        执行数据库迁移

        Args:
            progress_callback: 进度回调函数 (message, current, total)

        Returns:
            Dict: 迁移结果
        """
        async with self.migration_lock:
            start_time = datetime.now()

            try:
                # 初始化版本表
                await self.initialize_version_table()

                # 获取当前版本
                current_version = await self.get_db_version()

                if current_version >= self.CURRENT_VERSION:
                    return {
                        "success": True,
                        "message": "数据库已是最新版本，无需迁移",
                        "from_version": current_version,
                        "to_version": self.CURRENT_VERSION,
                        "duration": 0,
                    }

                logger.info(
                    f"开始数据库迁移: v{current_version} -> v{self.CURRENT_VERSION}"
                )

                # 迁移前自动备份，确保数据安全
                backup_path = await self.create_backup()
                if backup_path:
                    logger.info(f"迁移前备份已创建: {backup_path}")
                else:
                    logger.warning(
                        "迁移前备份失败，迁移将继续执行。请确认磁盘空间与文件权限。"
                    )

                # 执行迁移步骤
                migration_steps = []

                # 从版本1升级到版本2
                if current_version == 1:
                    migration_steps.append(self._migrate_v1_to_v2)

                # 从版本2升级到版本3
                if current_version <= 2:
                    migration_steps.append(self._migrate_v2_to_v3)

                # 从版本3升级到版本4
                if current_version <= 3:
                    migration_steps.append(self._migrate_v3_to_v4)

                # 从版本4升级到版本5
                if current_version <= 4:
                    migration_steps.append(self._migrate_v4_to_v5)

                # 从版本5升级到版本6
                if current_version <= 5:
                    migration_steps.append(self._migrate_v5_to_v6)

                # 从版本6升级到版本7
                if current_version <= 6:
                    migration_steps.append(self._migrate_v6_to_v7)

                # 从版本7升级到版本8
                if current_version <= 7:
                    migration_steps.append(self._migrate_v7_to_v8)

                # 从版本8升级到版本9
                if current_version <= 8:
                    migration_steps.append(self._migrate_v8_to_v9)

                # 执行所有迁移步骤
                for step in migration_steps:
                    await step(progress_callback)

                # 计算耗时
                duration = (datetime.now() - start_time).total_seconds()

                # 更新版本号
                await self.set_db_version(
                    self.CURRENT_VERSION,
                    self.VERSION_HISTORY.get(self.CURRENT_VERSION, ""),
                    duration,
                )

                logger.info(f"数据库迁移成功完成，耗时: {duration:.2f}秒")

                return {
                    "success": True,
                    "message": f"数据库迁移成功: v{current_version} -> v{self.CURRENT_VERSION}",
                    "from_version": current_version,
                    "to_version": self.CURRENT_VERSION,
                    "duration": duration,
                    "backup_path": backup_path,
                }

            except Exception as e:
                logger.error(f"数据库迁移失败: {e}", exc_info=True)
                return {
                    "success": False,
                    "message": f"数据库迁移失败: {str(e)}",
                    "error": str(e),
                }

    async def _migrate_v1_to_v2(
        self,
        progress_callback: Callable[[str, int, int], None] | None,
    ):
        """
        从版本1迁移到版本2
        主要变更：重建BM25索引和向量索引以支持新的检索架构
        """
        logger.info("执行迁移步骤: v1 -> v2 (重建索引)")

        try:
            # 检查是否有documents表
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute("""
                    SELECT COUNT(*) FROM sqlite_master
                    WHERE type='table' AND name='documents'
                """)
                has_table_row = await cursor.fetchone()
                has_table = (
                    has_table_row[0] if has_table_row and len(has_table_row) > 0 else 0
                ) > 0

                if not has_table:
                    logger.info("未找到 documents 表，按新数据库处理")
                    return

                # 获取文档总数
                cursor = await db.execute("SELECT COUNT(*) FROM documents")
                total_docs_row = await cursor.fetchone()
                total_docs = total_docs_row[0] if total_docs_row else 0

                if total_docs == 0:
                    logger.info("数据库为空，无需重建索引")
                    return

                logger.info(f"发现 {total_docs} 条 v1 数据，标记待重建索引")

                # 获取所有文档数据
                cursor = await db.execute("SELECT id, text, metadata FROM documents")
                await cursor.fetchall()

            # 重建索引需要在插件初始化完成后进行
            # 这里只记录需要重建的标记，实际重建在插件启动时处理
            logger.warning(f"检测到 {total_docs} 条 v1 迁移数据需要重建索引")
            logger.warning("请在插件初始化完成后，使用 WebUI 数据迁移功能或执行命令:")
            logger.warning("/lmem rebuild-index")
            logger.info(f"数据库迁移完成（{total_docs} 条文档已保留在 documents 表）")

            # 创建迁移状态标记
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS migration_status (
                        key TEXT PRIMARY KEY,
                        value TEXT,
                        updated_at TEXT
                    )
                """)
                await db.execute(
                    """
                    INSERT OR REPLACE INTO migration_status (key, value, updated_at)
                    VALUES (?, ?, ?)
                """,
                    ("needs_index_rebuild", "true", datetime.now(timezone.utc).isoformat()),
                )
                await db.execute(
                    """
                    INSERT OR REPLACE INTO migration_status (key, value, updated_at)
                    VALUES (?, ?, ?)
                """,
                    (
                        "pending_documents_count",
                        str(total_docs),
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                await db.commit()

        except Exception as e:
            logger.error(f"数据库迁移失败: {e}", exc_info=True)
            raise

    async def _migrate_v2_to_v3(
        self,
        progress_callback: Callable[[str, int, int], None] | None,
    ):
        """
        从版本2迁移到版本3
        主要变更：标记需要进行 session_id 格式升级

        策略说明：
        不在迁移阶段进行数据转换，原因：
        1. 大多数用户只有一个Bot，旧的session_id实际上就对应当前Bot的unified_msg_origin
        2. 迁移时无法获取运行时的platform信息，无法生成正确的unified_msg_origin
        3. 插件运行时会自动使用unified_msg_origin，旧数据保持不变不影响使用
        4. 只有多Bot用户才会遇到session_id冲突，这种情况下新消息会使用新格式

        此迁移步骤仅升级版本号，不进行实际数据转换。
        """
        logger.info("执行迁移步骤: v2 -> v3 (session_id 格式升级)")

        try:
            logger.info(
                "插件现在使用 unified_msg_origin (格式:platform:type:id) 作为会话标识"
            )
            logger.info("旧数据保持不变，新消息自动使用新格式")
            logger.info("对于单 Bot 用户，这不会导致任何问题")
            logger.info("对于多 Bot 用户，新旧数据会自然分离，避免混淆")

            logger.info("v2 -> v3 迁移完成")

        except Exception as e:
            logger.error(f"v2 -> v3 迁移失败: {e}", exc_info=True)
            raise

    async def _migrate_v3_to_v4(
        self,
        progress_callback: Callable[[str, int, int], None] | None,
    ):
        """
        从版本3迁移到版本4
        主要变更：
        - 旧记录 metadata 中补充 summary_schema_version=v1（标记为旧格式）
        - 新写入记录将自动携带 canonical_summary / persona_summary / source_window
        - 无法回填 source_window 的旧数据不做处理（traceable=false 由读取方判断）
        """
        logger.info("执行迁移步骤: v3 -> v4 (Schema v2 双通道总结字段)")

        try:
            async with aiosqlite.connect(self.db_path) as db:
                # 检查 documents 表是否存在
                cursor = await db.execute("""
                    SELECT COUNT(*) FROM sqlite_master
                    WHERE type='table' AND name='documents'
                """)
                row = await cursor.fetchone()
                if not row or row[0] == 0:
                    logger.info("未找到 documents 表，跳过 v4 迁移")
                    return

                # 为没有 summary_schema_version 的旧记录打上 v1 标记
                # 使用 JSON 函数更新 metadata 字段
                cursor = await db.execute(
                    "SELECT COUNT(*) FROM documents WHERE metadata IS NULL OR metadata NOT LIKE '%summary_schema_version%'"
                )
                count_row = await cursor.fetchone()
                legacy_count = count_row[0] if count_row else 0

                if legacy_count > 0:
                    logger.info(
                        f"发现 {legacy_count} 条旧格式记录，补充 summary_schema_version=v1 标记"
                    )

                    # 批量更新：将旧记录的 metadata 中注入 schema 版本标记
                    # 使用 COALESCE(NULLIF(...)) 处理 NULL/空字符串，再用 json_set 追加字段
                    await db.execute("""
                        UPDATE documents
                        SET metadata = json_set(
                            COALESCE(NULLIF(TRIM(COALESCE(metadata, '')), ''), '{}'),
                            '$.summary_schema_version', 'v1',
                            '$.summary_quality', 'unknown'
                        )
                        WHERE metadata IS NULL OR metadata NOT LIKE '%summary_schema_version%'
                    """)
                    await db.commit()
                    logger.info(f"已为 {legacy_count} 条旧记录补充 schema 版本标记")
                else:
                    logger.info("所有记录已有 summary_schema_version，无需补充")

            logger.info("v3 -> v4 迁移完成")

        except Exception as e:
            logger.error(f"v3 -> v4 迁移失败: {e}", exc_info=True)
            raise

    async def _migrate_v4_to_v5(
        self,
        progress_callback: Callable[[str, int, int], None] | None,
    ):
        """
        Migrate from version 4 to version 5.

        Main changes:
        - Create graph-memory tables used by the dual-route retrieval layer
        - Keep legacy document memory data unchanged
        """
        logger.info("执行迁移步骤: v4 -> v5 (Graph memory tables)")

        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("PRAGMA foreign_keys = ON")
                await db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS graph_nodes (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        node_key TEXT NOT NULL UNIQUE,
                        node_type TEXT NOT NULL,
                        node_value TEXT NOT NULL,
                        canonical_value TEXT NOT NULL,
                        metadata TEXT DEFAULT '{}',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                await db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS graph_edges (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        edge_key TEXT NOT NULL UNIQUE,
                        source_node_id INTEGER NOT NULL,
                        target_node_id INTEGER NOT NULL,
                        relation_type TEXT NOT NULL,
                        source_memory_id INTEGER NOT NULL,
                        weight REAL NOT NULL DEFAULT 1.0,
                        confidence REAL NOT NULL DEFAULT 0.8,
                        status TEXT NOT NULL DEFAULT 'active',
                        metadata TEXT DEFAULT '{}',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        FOREIGN KEY(source_node_id) REFERENCES graph_nodes(id) ON DELETE CASCADE,
                        FOREIGN KEY(target_node_id) REFERENCES graph_nodes(id) ON DELETE CASCADE
                    )
                    """
                )
                await db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS graph_entries (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        entry_key TEXT NOT NULL UNIQUE,
                        source_memory_id INTEGER NOT NULL,
                        session_id TEXT,
                        persona_id TEXT,
                        entry_type TEXT NOT NULL,
                        relation_type TEXT,
                        content TEXT NOT NULL,
                        metadata TEXT DEFAULT '{}',
                        edge_id INTEGER,
                        vector_doc_id INTEGER,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        FOREIGN KEY(edge_id) REFERENCES graph_edges(id) ON DELETE CASCADE
                    )
                    """
                )
                await db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS graph_entry_nodes (
                        entry_id INTEGER NOT NULL,
                        node_id INTEGER NOT NULL,
                        PRIMARY KEY(entry_id, node_id),
                        FOREIGN KEY(entry_id) REFERENCES graph_entries(id) ON DELETE CASCADE,
                        FOREIGN KEY(node_id) REFERENCES graph_nodes(id) ON DELETE CASCADE
                    )
                    """
                )
                await db.execute(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS livingmemory_graph_entries_fts
                    USING fts5(content, entry_id UNINDEXED, tokenize='unicode61')
                    """
                )
                await db.execute(
                    "CREATE INDEX IF NOT EXISTS idx_graph_nodes_canonical ON graph_nodes(canonical_value)"
                )
                await db.execute(
                    "CREATE INDEX IF NOT EXISTS idx_graph_edges_memory_id ON graph_edges(source_memory_id)"
                )
                await db.execute(
                    "CREATE INDEX IF NOT EXISTS idx_graph_entries_memory_id ON graph_entries(source_memory_id)"
                )
                await db.commit()

            logger.info("v4 -> v5 迁移完成")

        except Exception as e:
            logger.error(f"v4 -> v5 迁移失败: {e}", exc_info=True)
            raise

    async def _migrate_v5_to_v6(
        self,
        progress_callback: Callable[[str, int, int], None] | None,
    ):
        """
        从版本5迁移到版本6
        主要变更：插件 FTS 表统一 livingmemory 前缀，旧 documents_fts 仅在精确匹配旧结构时重命名备份。
        """
        logger.info("执行迁移步骤: v5 -> v6 (FTS 表前缀化与旧表备份)")

        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS livingmemory_memories_fts
                    USING fts5(content, doc_id UNINDEXED, tokenize='unicode61')
                """)
                await db.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS livingmemory_graph_entries_fts
                    USING fts5(content, entry_id UNINDEXED, tokenize='unicode61')
                """)

                await self._copy_fts_rows_if_exists(
                    db,
                    source_table="memories_fts",
                    target_table="livingmemory_memories_fts",
                    columns=("doc_id", "content"),
                )
                await self._copy_fts_rows_if_exists(
                    db,
                    source_table="graph_entries_fts",
                    target_table="livingmemory_graph_entries_fts",
                    columns=("entry_id", "content"),
                )

                await self._backup_legacy_documents_fts_if_safe(db)

                await db.commit()
                logger.info("v5 -> v6 FTS 表前缀化完成")

        except Exception as e:
            logger.error(f"v5 -> v6 迁移失败: {e}", exc_info=True)
            raise

    async def _migrate_v6_to_v7(
        self,
        progress_callback: Callable[[str, int, int], None] | None,
    ):
        """Add storage indexes and run lightweight FTS maintenance."""
        logger.info("执行迁移步骤: v6 -> v7 (storage indexes and FTS maintenance)")

        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("PRAGMA busy_timeout = 10000")
                await db.execute("PRAGMA foreign_keys = ON")

                if await self._table_exists(db, "graph_edges"):
                    await db.execute(
                        """
                        CREATE INDEX IF NOT EXISTS idx_graph_edges_semantic
                        ON graph_edges(source_node_id, target_node_id, relation_type)
                        """
                    )
                if await self._table_exists(db, "graph_entries"):
                    await db.execute(
                        """
                        CREATE INDEX IF NOT EXISTS idx_graph_entries_scope_latest
                        ON graph_entries(session_id, persona_id, source_memory_id, id DESC)
                        """
                    )
                if await self._table_exists(db, "graph_entry_nodes"):
                    await db.execute(
                        "CREATE INDEX IF NOT EXISTS idx_graph_entry_nodes_node ON graph_entry_nodes(node_id)"
                    )
                if await self._table_exists(db, "memory_atoms"):
                    await db.execute(
                        "CREATE INDEX IF NOT EXISTS idx_atoms_persona ON memory_atoms(persona_id)"
                    )
                    await db.execute(
                        """
                        CREATE INDEX IF NOT EXISTS idx_atoms_scope_status
                        ON memory_atoms(status, session_id, persona_id)
                        """
                    )
                    await db.execute(
                        """
                        CREATE INDEX IF NOT EXISTS idx_atoms_status_expires
                        ON memory_atoms(status, expires_at)
                        """
                    )

                for table_name in (
                    "livingmemory_memories_fts",
                    "livingmemory_graph_entries_fts",
                    "memory_atoms_fts",
                ):
                    try:
                        await db.execute(
                            f"INSERT INTO {table_name}({table_name}) VALUES ('optimize')"
                        )
                    except Exception:
                        logger.debug(
                            f"跳过 FTS optimize: {table_name}",
                            exc_info=True,
                        )

                await db.commit()
                await db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

            logger.info("v6 -> v7 迁移完成")

        except Exception as e:
            logger.error(f"v6 -> v7 迁移失败: {e}", exc_info=True)
            raise

    async def _migrate_v7_to_v8(
        self,
        progress_callback: Callable[[str, int, int], None] | None,
    ):
        """Add write-operation log and expression indexes for hot metadata fields."""
        logger.info("执行迁移步骤: v7 -> v8 (write ops and hot metadata indexes)")

        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("PRAGMA busy_timeout = 10000")
                await db.execute(
                    """
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
                    """
                )
                await db.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_memory_write_ops_status
                    ON memory_write_ops(status, updated_at)
                    """
                )
                await db.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_memory_write_ops_memory
                    ON memory_write_ops(memory_id, op_type)
                    """
                )

                if await self._table_exists(db, "documents"):
                    await db.execute(
                        """
                        CREATE INDEX IF NOT EXISTS idx_doc_persona_metadata
                        ON documents(json_extract(metadata, '$.persona_id'))
                        """
                    )
                    await db.execute(
                        """
                        CREATE INDEX IF NOT EXISTS idx_doc_importance_metadata
                        ON documents(json_extract(metadata, '$.importance'))
                        """
                    )
                    await db.execute(
                        """
                        CREATE INDEX IF NOT EXISTS idx_doc_last_access_metadata
                        ON documents(json_extract(metadata, '$.last_access_time'))
                        """
                    )
                    await db.execute(
                        """
                        UPDATE documents
                        SET metadata = json_set(
                            COALESCE(NULLIF(TRIM(COALESCE(metadata, '')), ''), '{}'),
                            '$.access_count',
                            COALESCE(json_extract(metadata, '$.access_count'), 0)
                        )
                        WHERE json_valid(
                            COALESCE(NULLIF(TRIM(COALESCE(metadata, '')), ''), '{}')
                        )
                        """
                    )

                await db.commit()

            logger.info("v7 -> v8 迁移完成")

        except Exception as e:
            logger.error(f"v7 -> v8 迁移失败: {e}", exc_info=True)
            raise

    async def ensure_v9_schema(self) -> None:
        """幂等创建 v9 核心结构，不更新版本号、不执行数据回填。"""
        async with self.migration_lock:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("PRAGMA busy_timeout = 10000")
                await db.execute("PRAGMA foreign_keys = ON")
                await db.execute("BEGIN IMMEDIATE")
                try:
                    await self._apply_v9_schema(db)
                    await db.commit()
                except Exception:
                    await db.rollback()
                    raise

    async def _migrate_v8_to_v9(
        self,
        progress_callback: Callable[[str, int, int], None] | None,
    ):
        """Create owner-scoped evolving-memory structures using DDL only."""
        logger.info("执行迁移步骤: v8 -> v9 (owner-scoped evolving memory)")
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA busy_timeout = 10000")
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute("BEGIN IMMEDIATE")
            try:
                await self._apply_v9_schema(db)
                await db.commit()
            except Exception:
                await db.rollback()
                logger.error("v8 -> v9 迁移失败", exc_info=True)
                raise
        if progress_callback:
            progress_callback("v9 可演化记忆结构已创建", 1, 1)
        logger.info("v8 -> v9 迁移完成")

    async def _apply_v9_schema(self, db: aiosqlite.Connection) -> None:
        """Apply the complete v9 DDL idempotently inside the caller transaction."""
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS migration_status (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TEXT
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_owners (
                owner_user_id TEXT PRIMARY KEY,
                display_name TEXT,
                status TEXT NOT NULL DEFAULT 'active'
                    CHECK(status IN ('active', 'merged', 'disabled')),
                metadata TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_identity_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform_id TEXT NOT NULL,
                bot_id TEXT NOT NULL,
                external_user_id TEXT NOT NULL,
                owner_user_id TEXT NOT NULL,
                verified INTEGER NOT NULL DEFAULT 0 CHECK(verified IN (0, 1)),
                source TEXT NOT NULL DEFAULT 'automatic',
                status TEXT NOT NULL DEFAULT 'active'
                    CHECK(status IN ('active', 'revoked')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(platform_id, bot_id, external_user_id),
                FOREIGN KEY(owner_user_id) REFERENCES memory_owners(owner_user_id)
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_items (
                memory_item_id TEXT PRIMARY KEY,
                owner_user_id TEXT NOT NULL,
                scope TEXT NOT NULL
                    CHECK(scope IN ('user', 'persona', 'session', 'public', 'legacy_session')),
                session_id TEXT,
                persona_id TEXT,
                item_type TEXT NOT NULL DEFAULT 'fact',
                canonical_key TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active'
                    CHECK(status IN ('active', 'conflicted', 'archived', 'superseded')),
                current_revision_no INTEGER NOT NULL DEFAULT 1 CHECK(current_revision_no >= 1),
                version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
                current_document_id INTEGER,
                importance REAL NOT NULL DEFAULT 0.5 CHECK(importance >= 0.0 AND importance <= 1.0),
                confidence REAL NOT NULL DEFAULT 0.7 CHECK(confidence >= 0.0 AND confidence <= 1.0),
                useful_score REAL NOT NULL DEFAULT 0.0 CHECK(useful_score >= -1.0 AND useful_score <= 1.0),
                useful_count INTEGER NOT NULL DEFAULT 0 CHECK(useful_count >= 0),
                invalid_count INTEGER NOT NULL DEFAULT 0 CHECK(invalid_count >= 0),
                group_safe INTEGER NOT NULL DEFAULT 0 CHECK(group_safe IN (0, 1)),
                index_status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(index_status IN ('current', 'pending', 'needs_repair', 'disabled')),
                index_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                CHECK(scope != 'persona' OR persona_id IS NOT NULL),
                CHECK(scope NOT IN ('session', 'legacy_session') OR session_id IS NOT NULL),
                FOREIGN KEY(owner_user_id) REFERENCES memory_owners(owner_user_id)
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_item_revisions (
                revision_id TEXT PRIMARY KEY,
                memory_item_id TEXT NOT NULL,
                owner_user_id TEXT NOT NULL,
                revision_no INTEGER NOT NULL CHECK(revision_no >= 1),
                operation TEXT NOT NULL
                    CHECK(operation IN ('create', 'update', 'merge', 'supersede', 'archive', 'backfill', 'conflict')),
                content TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                structured_payload TEXT NOT NULL DEFAULT '{}',
                base_version INTEGER NOT NULL CHECK(base_version >= 0),
                actor_type TEXT NOT NULL
                    CHECK(actor_type IN ('automatic', 'admin', 'user', 'migration', 'system')),
                actor_id TEXT NOT NULL,
                reason TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(memory_item_id, revision_no),
                FOREIGN KEY(memory_item_id) REFERENCES memory_items(memory_item_id),
                FOREIGN KEY(owner_user_id) REFERENCES memory_owners(owner_user_id)
            )
            """
        )
        await db.execute(
            """
            CREATE TRIGGER IF NOT EXISTS trg_memory_item_revisions_immutable_update
            BEFORE UPDATE ON memory_item_revisions
            BEGIN
                SELECT RAISE(ABORT, 'memory_item_revisions are immutable');
            END
            """
        )
        await db.execute(
            """
            CREATE TRIGGER IF NOT EXISTS trg_memory_item_revisions_immutable_delete
            BEFORE DELETE ON memory_item_revisions
            BEGIN
                SELECT RAISE(ABORT, 'memory_item_revisions are immutable');
            END
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_item_sources (
                source_id TEXT PRIMARY KEY,
                source_key TEXT NOT NULL UNIQUE,
                owner_user_id TEXT NOT NULL,
                memory_item_id TEXT NOT NULL,
                revision_no INTEGER NOT NULL CHECK(revision_no >= 1),
                source_type TEXT NOT NULL,
                source_ref TEXT,
                document_id INTEGER,
                session_id TEXT,
                message_start_id INTEGER,
                message_end_id INTEGER,
                content_snapshot TEXT,
                availability TEXT NOT NULL DEFAULT 'available'
                    CHECK(availability IN ('available', 'partial', 'unavailable')),
                metadata TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                CHECK(message_start_id IS NULL OR message_end_id IS NULL OR message_start_id <= message_end_id),
                FOREIGN KEY(memory_item_id, revision_no)
                    REFERENCES memory_item_revisions(memory_item_id, revision_no),
                FOREIGN KEY(owner_user_id) REFERENCES memory_owners(owner_user_id)
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_item_relations (
                relation_id TEXT PRIMARY KEY,
                owner_user_id TEXT NOT NULL,
                source_item_id TEXT NOT NULL,
                target_item_id TEXT NOT NULL,
                relation_type TEXT NOT NULL
                    CHECK(relation_type IN (
                        'merged_into', 'supersedes', 'derived_from',
                        'duplicate_of', 'conflicts_with', 'related_to'
                    )),
                source_revision_no INTEGER,
                metadata TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                CHECK(source_item_id != target_item_id),
                UNIQUE(source_item_id, target_item_id, relation_type),
                FOREIGN KEY(source_item_id) REFERENCES memory_items(memory_item_id),
                FOREIGN KEY(target_item_id) REFERENCES memory_items(memory_item_id),
                FOREIGN KEY(owner_user_id) REFERENCES memory_owners(owner_user_id)
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_conflicts (
                conflict_id TEXT PRIMARY KEY,
                owner_user_id TEXT NOT NULL,
                left_item_id TEXT NOT NULL,
                right_item_id TEXT NOT NULL,
                conflict_type TEXT NOT NULL,
                severity TEXT NOT NULL DEFAULT 'medium'
                    CHECK(severity IN ('low', 'medium', 'high', 'critical')),
                status TEXT NOT NULL DEFAULT 'open'
                    CHECK(status IN ('open', 'resolved', 'dismissed')),
                resolution_action TEXT,
                resolved_by TEXT,
                resolution_note TEXT,
                metadata TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                resolved_at TEXT,
                CHECK(left_item_id != right_item_id),
                FOREIGN KEY(left_item_id) REFERENCES memory_items(memory_item_id),
                FOREIGN KEY(right_item_id) REFERENCES memory_items(memory_item_id),
                FOREIGN KEY(owner_user_id) REFERENCES memory_owners(owner_user_id)
            )
            """
        )
        await db.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS livingmemory_memory_items_fts
            USING fts5(
                content,
                canonical_key,
                memory_item_id UNINDEXED,
                owner_user_id UNINDEXED,
                item_type UNINDEXED,
                tokenize='unicode61'
            )
            """
        )

        await db.execute(
            """
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
                updated_at REAL NOT NULL,
                operation_key TEXT,
                entity_id TEXT
            )
            """
        )

        compatibility_columns = {
            "memory_atoms": (
                ("memory_item_id", "TEXT"),
                ("memory_revision_no", "INTEGER"),
            ),
            "graph_entries": (
                ("memory_item_id", "TEXT"),
                ("memory_revision_no", "INTEGER"),
                ("projection_status", "TEXT"),
            ),
            "memory_write_ops": (
                ("operation_key", "TEXT"),
                ("entity_id", "TEXT"),
            ),
        }
        for table_name, columns in compatibility_columns.items():
            if not await self._table_exists(db, table_name):
                continue
            for column_name, declaration in columns:
                await self._add_column_if_missing(
                    db,
                    table_name,
                    column_name,
                    declaration,
                )

        index_statements = (
            "CREATE INDEX IF NOT EXISTS idx_memory_identity_owner ON memory_identity_links(owner_user_id, status)",
            "CREATE INDEX IF NOT EXISTS idx_memory_items_owner_status ON memory_items(owner_user_id, status, updated_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_memory_items_owner_scope ON memory_items(owner_user_id, scope, persona_id, session_id, status)",
            "CREATE INDEX IF NOT EXISTS idx_memory_items_owner_canonical ON memory_items(owner_user_id, canonical_key, status)",
            "CREATE INDEX IF NOT EXISTS idx_memory_items_owner_hash ON memory_items(owner_user_id, content_hash, status)",
            "CREATE INDEX IF NOT EXISTS idx_memory_revisions_owner_item ON memory_item_revisions(owner_user_id, memory_item_id, revision_no DESC)",
            "CREATE INDEX IF NOT EXISTS idx_memory_sources_owner_item ON memory_item_sources(owner_user_id, memory_item_id, revision_no)",
            "CREATE INDEX IF NOT EXISTS idx_memory_sources_document ON memory_item_sources(document_id)",
            "CREATE INDEX IF NOT EXISTS idx_memory_relations_owner_source ON memory_item_relations(owner_user_id, source_item_id, relation_type)",
            "CREATE INDEX IF NOT EXISTS idx_memory_relations_owner_target ON memory_item_relations(owner_user_id, target_item_id, relation_type)",
            "CREATE INDEX IF NOT EXISTS idx_memory_conflicts_owner_status ON memory_conflicts(owner_user_id, status, severity, updated_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_memory_write_ops_status ON memory_write_ops(status, updated_at)",
            "CREATE INDEX IF NOT EXISTS idx_memory_write_ops_memory ON memory_write_ops(memory_id, op_type)",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_write_ops_operation_key ON memory_write_ops(operation_key) WHERE operation_key IS NOT NULL",
            "CREATE INDEX IF NOT EXISTS idx_memory_write_ops_entity ON memory_write_ops(entity_id, op_type)",
        )
        for statement in index_statements:
            await db.execute(statement)

        optional_index_statements = {
            "memory_atoms": (
                "CREATE INDEX IF NOT EXISTS idx_atoms_memory_item ON memory_atoms(memory_item_id, memory_revision_no)",
            ),
            "graph_entries": (
                "CREATE INDEX IF NOT EXISTS idx_graph_entries_memory_item ON graph_entries(memory_item_id, memory_revision_no, projection_status)",
            ),
        }
        for table_name, statements in optional_index_statements.items():
            if await self._table_exists(db, table_name):
                for statement in statements:
                    await db.execute(statement)

    async def _add_column_if_missing(
        self,
        db: aiosqlite.Connection,
        table_name: str,
        column_name: str,
        declaration: str,
    ) -> None:
        allowed_identifiers = {
            "memory_atoms",
            "graph_entries",
            "memory_write_ops",
            "memory_item_id",
            "memory_revision_no",
            "projection_status",
            "operation_key",
            "entity_id",
        }
        allowed_declarations = {"TEXT", "INTEGER"}
        if (
            table_name not in allowed_identifiers
            or column_name not in allowed_identifiers
            or declaration not in allowed_declarations
        ):
            raise ValueError("不允许的兼容字段 DDL")
        cursor = await db.execute(f'PRAGMA table_info("{table_name}")')
        existing_columns = {str(row[1]) for row in await cursor.fetchall()}
        if column_name not in existing_columns:
            await db.execute(
                f'ALTER TABLE "{table_name}" ADD COLUMN "{column_name}" {declaration}'
            )

    async def _table_exists(self, db: aiosqlite.Connection, table_name: str) -> bool:
        cursor = await db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        )
        return await cursor.fetchone() is not None

    async def _copy_fts_rows_if_exists(
        self,
        db: aiosqlite.Connection,
        source_table: str,
        target_table: str,
        columns: tuple[str, str],
    ):
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (source_table,),
        )
        if not await cursor.fetchone():
            return

        first_column, second_column = columns
        await db.execute(f"DELETE FROM {target_table}")
        await db.execute(
            f"""
            INSERT INTO {target_table}({first_column}, {second_column})
            SELECT {first_column}, {second_column} FROM {source_table}
            """
        )
        await db.execute(f"DROP TABLE IF EXISTS {source_table}")
        logger.info(f"已迁移并删除旧 FTS 表: {source_table} -> {target_table}")

    async def _backup_legacy_documents_fts_if_safe(self, db: aiosqlite.Connection):
        cursor = await db.execute("""
            SELECT sql FROM sqlite_master
            WHERE type='table' AND name='documents_fts'
        """)
        row = await cursor.fetchone()
        if not row:
            logger.info("未发现 documents_fts 表，跳过旧表备份")
            return

        if not await self._is_legacy_livingmemory_documents_fts(db, row[0] or ""):
            logger.warning(
                "documents_fts 不完全匹配旧 Memory FTS 结构，保留不处理"
            )
            return

        cursor = await db.execute("""
            SELECT name FROM sqlite_master
            WHERE type='table' AND name='livingmemory_legacy_documents_fts_backup'
        """)
        if await cursor.fetchone():
            logger.warning(
                "旧表备份 livingmemory_legacy_documents_fts_backup 已存在，保留 documents_fts 不处理"
            )
            return

        await db.execute(
            "ALTER TABLE documents_fts RENAME TO livingmemory_legacy_documents_fts_backup"
        )
        logger.warning(
            "已将旧 Memory documents_fts 重命名为 livingmemory_legacy_documents_fts_backup"
        )

    async def _is_legacy_livingmemory_documents_fts(
        self,
        db: aiosqlite.Connection,
        create_sql: str,
    ) -> bool:
        normalized_sql = " ".join(create_sql.lower().replace("\n", " ").split())
        expected_sql = "create virtual table documents_fts using fts5(content, doc_id, tokenize='unicode61')"
        if normalized_sql != expected_sql:
            return False

        cursor = await db.execute("PRAGMA table_xinfo(documents_fts)")
        rows = await cursor.fetchall()
        visible_columns = [row[1] for row in rows if int(row[6]) == 0]
        return visible_columns == ["content", "doc_id"]

    async def get_migration_info(self) -> dict[str, Any]:
        """
        获取迁移信息

        Returns:
            Dict: 迁移信息
        """
        try:
            current_version = await self.get_db_version()
            needs_migration = await self.needs_migration()

            # 获取迁移历史
            migration_history = []
            try:
                async with aiosqlite.connect(self.db_path) as db:
                    cursor = await db.execute("""
                        SELECT version, description, migrated_at, migration_duration_seconds
                        FROM db_version
                        ORDER BY id DESC
                        LIMIT 10
                    """)
                    rows = await cursor.fetchall()

                    for row in rows:
                        migration_history.append(
                            {
                                "version": row[0],
                                "description": row[1],
                                "migrated_at": row[2],
                                "duration": row[3],
                            }
                        )
            except Exception as e:
                logger.error(f"获取迁移历史失败: {e}", exc_info=True)

            return {
                "current_version": current_version,
                "latest_version": self.CURRENT_VERSION,
                "needs_migration": needs_migration,
                "version_history": self.VERSION_HISTORY,
                "migration_history": migration_history,
                "db_path": self.db_path,
            }

        except Exception as e:
            logger.error(f"获取迁移信息失败: {e}", exc_info=True)
            return {"error": str(e)}

    async def create_backup(self) -> str | None:
        """
        创建数据库备份

        Returns:
            Optional[str]: 备份文件路径，失败返回None
        """
        try:
            db_path = Path(self.db_path)
            backup_dir = db_path.parent / "backups"
            backup_dir.mkdir(parents=True, exist_ok=True)

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = (
                backup_dir / f"{db_path.stem}_backup_{timestamp}{db_path.suffix}"
            )

            logger.info(f"正在创建数据库备份: {backup_path}")

            # 使用SQLite的备份API
            async with aiosqlite.connect(self.db_path) as source:
                async with aiosqlite.connect(str(backup_path)) as dest:
                    await source.backup(dest)

            logger.info(f"数据库备份成功: {backup_path}")
            return str(backup_path)

        except Exception as e:
            logger.error(f"数据库备份失败: {e}", exc_info=True)
            return None
