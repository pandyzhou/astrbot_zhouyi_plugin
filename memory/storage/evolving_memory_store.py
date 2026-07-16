"""SQLite storage for owner-scoped evolving memory objects."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from contextlib import asynccontextmanager
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

import aiosqlite

from ..core.base.exceptions import (
    EvolvingMemoryAccessError,
    EvolvingMemoryIdempotencyError,
    EvolvingMemoryNotFoundError,
    EvolvingMemoryVersionConflictError,
)
from ..core.models.evolving_memory import (
    ConflictSeverity,
    ConflictStatus,
    DuplicateCandidate,
    IdentityLinkStatus,
    IndexStatus,
    MemoryAccessContext,
    MemoryAction,
    MemoryActorType,
    MemoryConflict,
    MemoryIdentityLink,
    MemoryItem,
    MemoryItemStatus,
    MemoryOwner,
    MemoryRelation,
    MemoryRelationType,
    MemoryRevision,
    MemoryScope,
    MemorySource,
    OwnerStatus,
    RevisionOperation,
    SourceAvailability,
    utc_now_iso,
)
from .db_migration import DBMigration


class EvolvingMemoryStore:
    """Canonical SQLite store with optimistic locking and transactional FTS."""

    _SORT_COLUMNS = {
        "updated_at": "mi.updated_at",
        "created_at": "mi.created_at",
        "importance": "mi.importance",
        "confidence": "mi.confidence",
        "useful_score": "mi.useful_score",
        "version": "mi.version",
    }
    _SORT_DIRECTIONS = {"asc": "ASC", "desc": "DESC"}

    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock_guard = asyncio.Lock()
        self._write_locks: dict[str, asyncio.Lock] = {}
        self._initialized = False

    @property
    def initialized(self) -> bool:
        return self._initialized

    async def initialize(self) -> None:
        await DBMigration(self.db_path).ensure_v9_schema()
        self._initialized = True

    @asynccontextmanager
    async def _connect(self):
        db = await aiosqlite.connect(self.db_path)
        db.row_factory = aiosqlite.Row
        try:
            await db.execute("PRAGMA journal_mode = WAL")
            await db.execute("PRAGMA busy_timeout = 10000")
            await db.execute("PRAGMA foreign_keys = ON")
            yield db
        finally:
            await db.close()

    async def _get_write_lock(self, owner_user_id: str, entity_key: str) -> asyncio.Lock:
        lock_key = f"{owner_user_id}\x1f{entity_key}"
        async with self._lock_guard:
            lock = self._write_locks.get(lock_key)
            if lock is None:
                lock = asyncio.Lock()
                self._write_locks[lock_key] = lock
            return lock

    @asynccontextmanager
    async def _write_transaction(self, owner_user_id: str, entity_key: str):
        lock = await self._get_write_lock(owner_user_id, entity_key)
        async with lock:
            async with self._connect() as db:
                await db.execute("BEGIN IMMEDIATE")
                try:
                    yield db
                    await db.commit()
                except Exception:
                    await db.rollback()
                    raise

    @staticmethod
    def _json_dump(value: Any) -> str:
        return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _json_object(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        if not value:
            return {}
        try:
            loaded = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return {}
        return loaded if isinstance(loaded, dict) else {}

    @classmethod
    def _normalize_operation_request(cls, value: Any) -> Any:
        if isinstance(value, Enum):
            return cls._normalize_operation_request(value.value)
        if isinstance(value, dict):
            normalized_items = sorted(
                ((str(key).strip(), item) for key, item in value.items()),
                key=lambda item: item[0],
            )
            return {
                key: cls._normalize_operation_request(item)
                for key, item in normalized_items
            }
        if isinstance(value, (list, tuple)):
            return [cls._normalize_operation_request(item) for item in value]
        if isinstance(value, (set, frozenset)):
            normalized = [cls._normalize_operation_request(item) for item in value]
            return sorted(
                normalized,
                key=lambda item: json.dumps(
                    item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ),
            )
        if isinstance(value, str):
            return value.strip()
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return str(value)

    @classmethod
    def _operation_request_digest(cls, request_payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
        normalized = cls._normalize_operation_request(request_payload)
        encoded = json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return normalized, hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def content_hash(content: str) -> str:
        normalized = " ".join(content.casefold().split())
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    @staticmethod
    def _new_id(prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex}"

    async def create_owner(
        self,
        *,
        owner_user_id: str | None = None,
        display_name: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryOwner:
        owner_id = (owner_user_id or f"owner_{uuid.uuid4().hex}").strip()
        if not owner_id:
            raise ValueError("owner_user_id 不得为空")
        now = utc_now_iso()
        async with self._write_transaction(owner_id, "owner") as db:
            await db.execute(
                """
                INSERT INTO memory_owners(
                    owner_user_id, display_name, status, metadata, created_at, updated_at
                ) VALUES (?, ?, 'active', ?, ?, ?)
                ON CONFLICT(owner_user_id) DO NOTHING
                """,
                (owner_id, display_name, self._json_dump(metadata), now, now),
            )
            owner = await self._get_owner_db(db, owner_id)
        if owner is None:
            raise EvolvingMemoryNotFoundError("owner 创建失败")
        return owner

    async def get_owner(self, owner_user_id: str) -> MemoryOwner | None:
        async with self._connect() as db:
            return await self._get_owner_db(db, owner_user_id)

    async def _get_owner_db(
        self, db: aiosqlite.Connection, owner_user_id: str
    ) -> MemoryOwner | None:
        cursor = await db.execute(
            "SELECT * FROM memory_owners WHERE owner_user_id = ?",
            (owner_user_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return MemoryOwner(
            owner_user_id=str(row["owner_user_id"]),
            display_name=row["display_name"],
            status=OwnerStatus(str(row["status"])),
            metadata=self._json_object(row["metadata"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    async def list_owners(
        self,
        *,
        statuses: Iterable[OwnerStatus] | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> list[MemoryOwner]:
        filters: list[str] = []
        params: list[Any] = []
        normalized_statuses = tuple(statuses or ())
        if normalized_statuses:
            placeholders = ",".join("?" for _ in normalized_statuses)
            filters.append(f"status IN ({placeholders})")
            params.extend(status.value for status in normalized_statuses)
        where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""
        params.extend((max(1, min(int(limit), 1000)), max(0, int(offset))))
        async with self._connect() as db:
            cursor = await db.execute(
                f"""
                SELECT * FROM memory_owners
                {where_clause}
                ORDER BY updated_at DESC, owner_user_id ASC
                LIMIT ? OFFSET ?
                """,
                params,
            )
            rows = await cursor.fetchall()
        return [
            MemoryOwner(
                owner_user_id=str(row["owner_user_id"]),
                display_name=row["display_name"],
                status=OwnerStatus(str(row["status"])),
                metadata=self._json_object(row["metadata"]),
                created_at=str(row["created_at"]),
                updated_at=str(row["updated_at"]),
            )
            for row in rows
        ]

    async def update_owner(
        self,
        *,
        owner_user_id: str,
        display_name: str | None,
        status: OwnerStatus,
        expected_updated_at: str | None = None,
    ) -> MemoryOwner:
        async with self._write_transaction(owner_user_id, "owner") as db:
            current = await self._get_owner_db(db, owner_user_id)
            if current is None:
                raise EvolvingMemoryNotFoundError("owner 不存在")
            if expected_updated_at is not None and current.updated_at != expected_updated_at:
                raise EvolvingMemoryIdempotencyError("owner 状态已变化，请重新加载")
            now = utc_now_iso()
            await db.execute(
                """
                UPDATE memory_owners
                SET display_name = ?, status = ?, updated_at = ?
                WHERE owner_user_id = ?
                """,
                (display_name, status.value, now, owner_user_id),
            )
            updated = await self._get_owner_db(db, owner_user_id)
            if updated is None:
                raise EvolvingMemoryNotFoundError("owner 更新失败")
            return updated

    async def get_identity_link_by_id(
        self, identity_link_id: int
    ) -> MemoryIdentityLink | None:
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT * FROM memory_identity_links WHERE id = ?",
                (int(identity_link_id),),
            )
            row = await cursor.fetchone()
        return self._row_to_identity(row) if row else None

    async def move_identity_link(
        self,
        *,
        identity_link_id: int,
        owner_user_id: str,
        expected_owner_user_id: str,
    ) -> MemoryIdentityLink:
        lock_key = f"identity-link:{int(identity_link_id)}"
        async with self._write_transaction("identity-link", lock_key) as db:
            target_owner = await self._get_owner_db(db, owner_user_id)
            if target_owner is None:
                raise EvolvingMemoryNotFoundError("目标 owner 不存在")
            if target_owner.status != OwnerStatus.ACTIVE:
                raise EvolvingMemoryAccessError("目标 owner 当前不可写")
            cursor = await db.execute(
                "SELECT * FROM memory_identity_links WHERE id = ?",
                (int(identity_link_id),),
            )
            row = await cursor.fetchone()
            if row is None:
                raise EvolvingMemoryNotFoundError("identity link 不存在")
            current = self._row_to_identity(row)
            if current.owner_user_id != expected_owner_user_id:
                raise EvolvingMemoryIdempotencyError("identity link owner 已变化")
            if current.owner_user_id == owner_user_id:
                return current
            now = utc_now_iso()
            await db.execute(
                """
                UPDATE memory_identity_links
                SET owner_user_id = ?, status = 'active', source = 'admin', updated_at = ?
                WHERE id = ? AND owner_user_id = ?
                """,
                (owner_user_id, now, int(identity_link_id), expected_owner_user_id),
            )
            await db.execute(
                "UPDATE memory_owners SET updated_at = ? WHERE owner_user_id IN (?, ?)",
                (now, expected_owner_user_id, owner_user_id),
            )
            cursor = await db.execute(
                "SELECT * FROM memory_identity_links WHERE id = ?",
                (int(identity_link_id),),
            )
            updated_row = await cursor.fetchone()
            if updated_row is None:
                raise EvolvingMemoryNotFoundError("identity link 移动失败")
            return self._row_to_identity(updated_row)

    @classmethod
    def _owner_merge_preview_id(
        cls,
        survivor_owner_user_id: str,
        source_owner_user_ids: list[str],
        expected_owner_states: dict[str, dict[str, str]],
    ) -> str:
        payload = {
            "survivor_owner_user_id": survivor_owner_user_id,
            "source_owner_user_ids": sorted(source_owner_user_ids),
            "expected_owner_states": expected_owner_states,
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return "owner_merge_" + hashlib.sha256(encoded).hexdigest()

    async def _owner_merge_state_db(
        self,
        db: aiosqlite.Connection,
        owner: MemoryOwner,
    ) -> dict[str, str]:
        async def aggregate(query: str) -> tuple[int, str, int]:
            row = await (await db.execute(query, (owner.owner_user_id,))).fetchone()
            return (
                int(row[0] or 0),
                str(row[1] or ""),
                int(row[2] or 0),
            )

        alias_count, alias_updated_at, _alias_version = await aggregate(
            "SELECT COUNT(*), COALESCE(MAX(updated_at), ''), 0 "
            "FROM memory_identity_links WHERE owner_user_id = ?"
        )
        item_count, item_updated_at, item_version_sum = await aggregate(
            "SELECT COUNT(*), COALESCE(MAX(updated_at), ''), COALESCE(SUM(version), 0) "
            "FROM memory_items WHERE owner_user_id = ?"
        )
        conflict_count, conflict_updated_at, _conflict_version = await aggregate(
            "SELECT COUNT(*), COALESCE(MAX(updated_at), ''), 0 "
            "FROM memory_conflicts WHERE owner_user_id = ?"
        )
        return {
            "status": owner.status.value,
            "updated_at": owner.updated_at,
            "alias_count": str(alias_count),
            "alias_updated_at": alias_updated_at,
            "memory_item_count": str(item_count),
            "memory_item_updated_at": item_updated_at,
            "memory_version_sum": str(item_version_sum),
            "conflict_count": str(conflict_count),
            "conflict_updated_at": conflict_updated_at,
        }

    async def preview_owner_merge(
        self,
        *,
        survivor_owner_user_id: str,
        source_owner_user_ids: list[str],
    ) -> dict[str, Any]:
        normalized_sources = sorted(
            {owner_id.strip() for owner_id in source_owner_user_ids if owner_id.strip()}
        )
        if survivor_owner_user_id in normalized_sources:
            normalized_sources.remove(survivor_owner_user_id)
        if not normalized_sources:
            raise ValueError("owner merge 至少需要一个来源 owner")
        owner_ids = [survivor_owner_user_id, *normalized_sources]
        placeholders = ",".join("?" for _ in owner_ids)
        async with self._connect() as db:
            cursor = await db.execute(
                f"SELECT * FROM memory_owners WHERE owner_user_id IN ({placeholders})",
                owner_ids,
            )
            owner_rows = await cursor.fetchall()
            owners = {
                str(row["owner_user_id"]): MemoryOwner(
                    owner_user_id=str(row["owner_user_id"]),
                    display_name=row["display_name"],
                    status=OwnerStatus(str(row["status"])),
                    metadata=self._json_object(row["metadata"]),
                    created_at=str(row["created_at"]),
                    updated_at=str(row["updated_at"]),
                )
                for row in owner_rows
            }
            if set(owners) != set(owner_ids):
                raise EvolvingMemoryNotFoundError("owner merge 包含不存在的 owner")
            if any(owner.status != OwnerStatus.ACTIVE for owner in owners.values()):
                raise EvolvingMemoryAccessError("仅 active owner 可以合并")
            source_placeholders = ",".join("?" for _ in normalized_sources)
            alias_count = int(
                (
                    await (
                        await db.execute(
                            f"SELECT COUNT(*) FROM memory_identity_links WHERE owner_user_id IN ({source_placeholders}) AND status = 'active'",
                            normalized_sources,
                        )
                    ).fetchone()
                )[0]
            )
            memory_item_count = int(
                (
                    await (
                        await db.execute(
                            f"SELECT COUNT(*) FROM memory_items WHERE owner_user_id IN ({source_placeholders})",
                            normalized_sources,
                        )
                    ).fetchone()
                )[0]
            )
            conflict_count = int(
                (
                    await (
                        await db.execute(
                            f"SELECT COUNT(*) FROM memory_conflicts WHERE owner_user_id IN ({source_placeholders}) AND status = 'open'",
                            normalized_sources,
                        )
                    ).fetchone()
                )[0]
            )
            expected_states = {
                owner_id: await self._owner_merge_state_db(db, owners[owner_id])
                for owner_id in sorted(owner_ids)
            }
        preview_id = self._owner_merge_preview_id(
            survivor_owner_user_id,
            normalized_sources,
            expected_states,
        )
        warnings = []
        if conflict_count:
            warnings.append("来源 owner 存在未解决冲突，合并后仍需逐条处理")
        return {
            "preview_id": preview_id,
            "survivor_owner_user_id": survivor_owner_user_id,
            "source_owner_user_ids": normalized_sources,
            "alias_count": alias_count,
            "memory_item_count": memory_item_count,
            "conflict_count": conflict_count,
            "warnings": warnings,
            "expected_owner_states": expected_states,
        }

    async def _rewrite_revision_owners_db(
        self,
        db: aiosqlite.Connection,
        *,
        survivor_owner_user_id: str,
        source_owner_user_ids: list[str],
    ) -> None:
        placeholders = ",".join("?" for _ in source_owner_user_ids)
        await db.execute("DROP TRIGGER IF EXISTS trg_memory_item_revisions_immutable_update")
        try:
            await db.execute(
                f"""
                UPDATE memory_item_revisions
                SET owner_user_id = ?
                WHERE memory_item_id IN (
                    SELECT memory_item_id
                    FROM memory_items
                    WHERE owner_user_id IN ({placeholders})
                )
                  AND owner_user_id != ?
                """,
                (
                    survivor_owner_user_id,
                    *source_owner_user_ids,
                    survivor_owner_user_id,
                ),
            )
            cursor = await db.execute(
                f"""
                SELECT COUNT(*)
                FROM memory_item_revisions revisions
                JOIN memory_items items
                  ON items.memory_item_id = revisions.memory_item_id
                WHERE items.owner_user_id IN ({placeholders})
                  AND revisions.owner_user_id != ?
                """,
                (*source_owner_user_ids, survivor_owner_user_id),
            )
            mismatch_count = int((await cursor.fetchone())[0])
            if mismatch_count:
                raise EvolvingMemoryAccessError("owner merge 后 revision owner 审计不一致")
        finally:
            await db.execute(
                """
                CREATE TRIGGER IF NOT EXISTS trg_memory_item_revisions_immutable_update
                BEFORE UPDATE ON memory_item_revisions
                BEGIN
                    SELECT RAISE(ABORT, 'memory_item_revisions are immutable');
                END
                """
            )

    async def merge_owners(
        self,
        *,
        survivor_owner_user_id: str,
        source_owner_user_ids: list[str],
        preview_id: str,
        expected_owner_states: dict[str, dict[str, str]],
        operation_key: str,
    ) -> dict[str, Any]:
        normalized_sources = sorted(
            {
                owner_id.strip()
                for owner_id in source_owner_user_ids
                if owner_id.strip() and owner_id.strip() != survivor_owner_user_id
            }
        )
        if not normalized_sources:
            raise ValueError("owner merge 至少需要一个来源 owner")
        expected_preview = self._owner_merge_preview_id(
            survivor_owner_user_id,
            normalized_sources,
            expected_owner_states,
        )
        operation_request = {
            "survivor_owner_user_id": survivor_owner_user_id,
            "source_owner_user_ids": normalized_sources,
            "preview_id": preview_id,
            "expected_owner_states": expected_owner_states,
        }
        result = {
            "merged": True,
            "survivor_owner_user_id": survivor_owner_user_id,
            "source_owner_user_ids": normalized_sources,
        }
        owner_ids = [survivor_owner_user_id, *normalized_sources]
        lock_key = "owner-merge:" + ":".join(sorted(owner_ids))
        async with self._write_transaction(survivor_owner_user_id, lock_key) as db:
            replay = await self._load_operation_db(
                db,
                operation_key=operation_key,
                owner_user_id=survivor_owner_user_id,
                operation_type="owner_merge",
                request_payload=operation_request,
            )
            if replay is not None:
                return result
            if not preview_id or preview_id != expected_preview:
                raise EvolvingMemoryIdempotencyError("owner merge preview 无效或已过期")
            owners: dict[str, MemoryOwner] = {}
            for owner_id in owner_ids:
                owner = await self._get_owner_db(db, owner_id)
                if owner is None:
                    raise EvolvingMemoryNotFoundError("owner merge 包含不存在的 owner")
                expected = expected_owner_states.get(owner_id)
                current_state = await self._owner_merge_state_db(db, owner)
                if expected != current_state:
                    raise EvolvingMemoryIdempotencyError("owner 状态已变化，请重新预览")
                if owner.status != OwnerStatus.ACTIVE:
                    raise EvolvingMemoryAccessError("仅 active owner 可以合并")
                owners[owner_id] = owner

            source_placeholders = ",".join("?" for _ in normalized_sources)
            now = utc_now_iso()
            await self._rewrite_revision_owners_db(
                db,
                survivor_owner_user_id=survivor_owner_user_id,
                source_owner_user_ids=normalized_sources,
            )
            table_columns = (
                ("memory_items", "owner_user_id"),
                ("memory_item_sources", "owner_user_id"),
                ("memory_item_relations", "owner_user_id"),
                ("memory_conflicts", "owner_user_id"),
            )
            for table_name, column_name in table_columns:
                await db.execute(
                    f"UPDATE {table_name} SET {column_name} = ? WHERE {column_name} IN ({source_placeholders})",
                    (survivor_owner_user_id, *normalized_sources),
                )
            await db.execute(
                f"UPDATE memory_identity_links SET owner_user_id = ?, source = 'admin', updated_at = ? WHERE owner_user_id IN ({source_placeholders})",
                (survivor_owner_user_id, now, *normalized_sources),
            )
            await db.execute(
                f"UPDATE livingmemory_memory_items_fts SET owner_user_id = ? WHERE owner_user_id IN ({source_placeholders})",
                (survivor_owner_user_id, *normalized_sources),
            )
            for source_id in normalized_sources:
                metadata = dict(owners[source_id].metadata)
                metadata["merged_into"] = survivor_owner_user_id
                await db.execute(
                    """
                    UPDATE memory_owners
                    SET status = 'merged', metadata = ?, updated_at = ?
                    WHERE owner_user_id = ?
                    """,
                    (self._json_dump(metadata), now, source_id),
                )
            await db.execute(
                "UPDATE memory_owners SET updated_at = ? WHERE owner_user_id = ?",
                (now, survivor_owner_user_id),
            )
            await self._record_operation_db(
                db,
                operation_key=operation_key,
                action=None,
                owner_user_id=survivor_owner_user_id,
                entity_id=survivor_owner_user_id,
                version=0,
                request_payload=operation_request,
                op_type="owner_merge",
                extra={"source_owner_user_ids": normalized_sources},
            )
        return result

    async def resolve_identity(
        self,
        *,
        platform_id: str,
        bot_id: str,
        external_user_id: str,
        create_if_missing: bool = True,
    ) -> MemoryIdentityLink | None:
        platform = platform_id.strip()
        bot = bot_id.strip()
        external = external_user_id.strip()
        if not platform or not bot or not external:
            raise ValueError("platform_id、bot_id、external_user_id 均不得为空")
        identity_key = f"identity:{platform}:{bot}:{external}"
        async with self._write_transaction("identity-resolution", identity_key) as db:
            existing = await self._get_identity_link_db(db, platform, bot, external)
            if existing is not None:
                return existing if existing.status == IdentityLinkStatus.ACTIVE else None
            if not create_if_missing:
                return None

            owner_id = f"owner_{uuid.uuid4().hex}"
            now = utc_now_iso()
            await db.execute(
                """
                INSERT INTO memory_owners(
                    owner_user_id, display_name, status, metadata, created_at, updated_at
                ) VALUES (?, NULL, 'active', '{}', ?, ?)
                """,
                (owner_id, now, now),
            )
            cursor = await db.execute(
                """
                INSERT INTO memory_identity_links(
                    platform_id, bot_id, external_user_id, owner_user_id,
                    verified, source, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 0, 'automatic', 'active', ?, ?)
                """,
                (platform, bot, external, owner_id, now, now),
            )
            return MemoryIdentityLink(
                identity_link_id=int(cursor.lastrowid),
                owner_user_id=owner_id,
                platform_id=platform,
                bot_id=bot,
                external_user_id=external,
                verified=False,
                source="automatic",
                status=IdentityLinkStatus.ACTIVE,
                created_at=now,
                updated_at=now,
            )

    async def link_identity(
        self,
        *,
        owner_user_id: str,
        platform_id: str,
        bot_id: str,
        external_user_id: str,
        verified: bool,
        source: str,
    ) -> MemoryIdentityLink:
        platform = platform_id.strip()
        bot = bot_id.strip()
        external = external_user_id.strip()
        identity_key = f"identity:{platform}:{bot}:{external}"
        async with self._write_transaction(owner_user_id, identity_key) as db:
            if await self._get_owner_db(db, owner_user_id) is None:
                raise EvolvingMemoryNotFoundError("owner 不存在")
            existing = await self._get_identity_link_db(db, platform, bot, external)
            if existing is not None:
                if existing.owner_user_id != owner_user_id:
                    raise EvolvingMemoryAccessError("身份已绑定到其他 owner，禁止隐式移动")
                return existing
            now = utc_now_iso()
            cursor = await db.execute(
                """
                INSERT INTO memory_identity_links(
                    platform_id, bot_id, external_user_id, owner_user_id,
                    verified, source, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)
                """,
                (platform, bot, external, owner_user_id, int(verified), source, now, now),
            )
            return MemoryIdentityLink(
                identity_link_id=int(cursor.lastrowid),
                owner_user_id=owner_user_id,
                platform_id=platform,
                bot_id=bot,
                external_user_id=external,
                verified=bool(verified),
                source=source,
                status=IdentityLinkStatus.ACTIVE,
                created_at=now,
                updated_at=now,
            )

    async def _get_identity_link_db(
        self,
        db: aiosqlite.Connection,
        platform_id: str,
        bot_id: str,
        external_user_id: str,
    ) -> MemoryIdentityLink | None:
        cursor = await db.execute(
            """
            SELECT * FROM memory_identity_links
            WHERE platform_id = ? AND bot_id = ? AND external_user_id = ?
            """,
            (platform_id, bot_id, external_user_id),
        )
        row = await cursor.fetchone()
        return self._row_to_identity(row) if row else None

    async def list_identity_links(self, owner_user_id: str) -> list[MemoryIdentityLink]:
        async with self._connect() as db:
            cursor = await db.execute(
                """
                SELECT * FROM memory_identity_links
                WHERE owner_user_id = ?
                ORDER BY platform_id, bot_id, external_user_id
                """,
                (owner_user_id,),
            )
            rows = await cursor.fetchall()
        return [self._row_to_identity(row) for row in rows]

    def _row_to_identity(self, row: aiosqlite.Row) -> MemoryIdentityLink:
        return MemoryIdentityLink(
            identity_link_id=int(row["id"]),
            owner_user_id=str(row["owner_user_id"]),
            platform_id=str(row["platform_id"]),
            bot_id=str(row["bot_id"]),
            external_user_id=str(row["external_user_id"]),
            verified=bool(row["verified"]),
            source=str(row["source"]),
            status=IdentityLinkStatus(str(row["status"])),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def _validate_scope_access(
        self,
        context: MemoryAccessContext,
        scope: MemoryScope,
        session_id: str | None,
        persona_id: str | None,
        group_safe: bool,
    ) -> None:
        if scope not in context.allowed_scopes:
            raise EvolvingMemoryAccessError(f"当前上下文不允许 {scope.value} scope")
        if scope == MemoryScope.PUBLIC and not context.allow_public:
            raise EvolvingMemoryAccessError("当前上下文不允许 public scope")
        if scope == MemoryScope.LEGACY_SESSION and not context.allow_legacy_session:
            raise EvolvingMemoryAccessError("legacy_session 仅允许内部迁移")
        if scope == MemoryScope.PERSONA and persona_id != context.persona_id:
            raise EvolvingMemoryAccessError("persona scope 必须绑定当前 persona")
        if scope in {MemoryScope.SESSION, MemoryScope.LEGACY_SESSION}:
            if session_id != context.session_id:
                raise EvolvingMemoryAccessError("session scope 必须绑定当前完整会话 ID")
        if context.is_group and scope in {MemoryScope.USER, MemoryScope.PERSONA} and not group_safe:
            raise EvolvingMemoryAccessError("群聊不可写入未标记 group_safe 的跨会话记忆")

    async def create_item(
        self,
        *,
        context: MemoryAccessContext,
        scope: MemoryScope,
        content: str,
        canonical_key: str,
        item_type: str,
        expected_version: int,
        operation_key: str,
        actor_type: MemoryActorType,
        actor_id: str,
        reason: str | None = None,
        structured_payload: dict[str, Any] | None = None,
        session_id: str | None = None,
        persona_id: str | None = None,
        importance: float = 0.5,
        confidence: float = 0.7,
        group_safe: bool = False,
        memory_item_id: str | None = None,
        revision_operation: RevisionOperation = RevisionOperation.CREATE,
        source: dict[str, Any] | None = None,
    ) -> tuple[MemoryItem, bool]:
        if expected_version != 0:
            raise EvolvingMemoryVersionConflictError(memory_item_id or "new", expected_version, 0)
        normalized_content = content.strip()
        normalized_key = canonical_key.strip()
        if not normalized_content or not normalized_key:
            raise ValueError("content 和 canonical_key 不得为空")
        effective_session = session_id or (
            context.session_id if scope in {MemoryScope.SESSION, MemoryScope.LEGACY_SESSION} else None
        )
        effective_persona = persona_id or (
            context.persona_id if scope == MemoryScope.PERSONA else None
        )
        self._validate_scope_access(
            context, scope, effective_session, effective_persona, group_safe
        )
        operation_request = {
            "memory_item_id": memory_item_id,
            "expected_version": expected_version,
            "scope": scope.value,
            "session_id": effective_session,
            "persona_id": effective_persona,
            "content": normalized_content,
            "canonical_key": normalized_key,
            "item_type": item_type,
            "structured_payload": structured_payload or {},
            "importance": float(importance),
            "confidence": float(confidence),
            "group_safe": bool(group_safe),
            "actor_type": actor_type.value,
            "actor_id": actor_id,
            "reason": reason,
            "revision_operation": revision_operation.value,
            "source": source,
        }
        item_id = memory_item_id or self._new_id("mem")
        async with self._write_transaction(context.owner_user_id, item_id) as db:
            replay = await self._load_idempotent_item(
                db,
                operation_key=operation_key,
                owner_user_id=context.owner_user_id,
                action=MemoryAction.CREATE,
                request_payload=operation_request,
            )
            if replay is not None:
                return replay, True
            if await self._get_item_db(db, context.owner_user_id, item_id) is not None:
                raise EvolvingMemoryIdempotencyError("memory_item_id 已存在但 operation_key 不匹配")
            item = await self._insert_item_db(
                db,
                memory_item_id=item_id,
                owner_user_id=context.owner_user_id,
                scope=scope,
                session_id=effective_session,
                persona_id=effective_persona,
                item_type=item_type,
                canonical_key=normalized_key,
                content=normalized_content,
                structured_payload=structured_payload or {},
                importance=importance,
                confidence=confidence,
                group_safe=group_safe,
                actor_type=actor_type,
                actor_id=actor_id,
                reason=reason,
                revision_operation=revision_operation,
            )
            if source:
                await self._insert_source_db(
                    db,
                    owner_user_id=context.owner_user_id,
                    memory_item_id=item_id,
                    revision_no=1,
                    source=source,
                )
            await self._record_operation_db(
                db,
                operation_key=operation_key,
                action=MemoryAction.CREATE,
                owner_user_id=context.owner_user_id,
                entity_id=item_id,
                version=1,
                request_payload=operation_request,
            )
            return item, False

    async def _insert_item_db(
        self,
        db: aiosqlite.Connection,
        *,
        memory_item_id: str,
        owner_user_id: str,
        scope: MemoryScope,
        session_id: str | None,
        persona_id: str | None,
        item_type: str,
        canonical_key: str,
        content: str,
        structured_payload: dict[str, Any],
        importance: float,
        confidence: float,
        group_safe: bool,
        actor_type: MemoryActorType,
        actor_id: str,
        reason: str | None,
        revision_operation: RevisionOperation,
    ) -> MemoryItem:
        now = utc_now_iso()
        digest = self.content_hash(content)
        await db.execute(
            """
            INSERT INTO memory_items(
                memory_item_id, owner_user_id, scope, session_id, persona_id,
                item_type, canonical_key, content_hash, status,
                current_revision_no, version, current_document_id,
                importance, confidence, useful_score, useful_count, invalid_count,
                group_safe, index_status, index_error, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', 1, 1, NULL,
                      ?, ?, 0.0, 0, 0, ?, 'pending', NULL, ?, ?)
            """,
            (
                memory_item_id,
                owner_user_id,
                scope.value,
                session_id,
                persona_id,
                item_type,
                canonical_key,
                digest,
                float(importance),
                float(confidence),
                int(group_safe),
                now,
                now,
            ),
        )
        await db.execute(
            """
            INSERT INTO memory_item_revisions(
                revision_id, memory_item_id, owner_user_id, revision_no,
                operation, content, content_hash, structured_payload,
                base_version, actor_type, actor_id, reason, created_at
            ) VALUES (?, ?, ?, 1, ?, ?, ?, ?, 0, ?, ?, ?, ?)
            """,
            (
                self._new_id("rev"),
                memory_item_id,
                owner_user_id,
                revision_operation.value,
                content,
                digest,
                self._json_dump(structured_payload),
                actor_type.value,
                actor_id,
                reason,
                now,
            ),
        )
        await self._refresh_fts_db(
            db,
            memory_item_id=memory_item_id,
            owner_user_id=owner_user_id,
            status=MemoryItemStatus.ACTIVE,
            content=content,
            canonical_key=canonical_key,
            item_type=item_type,
        )
        item = await self._get_item_db(db, owner_user_id, memory_item_id)
        if item is None:
            raise EvolvingMemoryNotFoundError("记忆对象创建后无法读取")
        return item

    async def get_item(
        self,
        *,
        owner_user_id: str,
        memory_item_id: str,
        context: MemoryAccessContext | None = None,
    ) -> MemoryItem | None:
        async with self._connect() as db:
            item = await self._get_item_db(db, owner_user_id, memory_item_id)
        if item is not None and context is not None and not context.can_access_item(item):
            raise EvolvingMemoryAccessError()
        return item

    async def _get_item_db(
        self,
        db: aiosqlite.Connection,
        owner_user_id: str,
        memory_item_id: str,
    ) -> MemoryItem | None:
        cursor = await db.execute(
            """
            SELECT mi.*, r.content AS revision_content,
                   r.structured_payload AS revision_structured_payload
            FROM memory_items mi
            JOIN memory_item_revisions r
              ON r.memory_item_id = mi.memory_item_id
             AND r.revision_no = mi.current_revision_no
            WHERE mi.owner_user_id = ? AND mi.memory_item_id = ?
            """,
            (owner_user_id, memory_item_id),
        )
        row = await cursor.fetchone()
        return self._row_to_item(row) if row else None

    def _row_to_item(self, row: aiosqlite.Row) -> MemoryItem:
        return MemoryItem(
            memory_item_id=str(row["memory_item_id"]),
            owner_user_id=str(row["owner_user_id"]),
            scope=MemoryScope(str(row["scope"])),
            session_id=row["session_id"],
            persona_id=row["persona_id"],
            item_type=str(row["item_type"]),
            canonical_key=str(row["canonical_key"]),
            content_hash=str(row["content_hash"]),
            status=MemoryItemStatus(str(row["status"])),
            current_revision_no=int(row["current_revision_no"]),
            version=int(row["version"]),
            current_document_id=(
                int(row["current_document_id"])
                if row["current_document_id"] is not None
                else None
            ),
            importance=float(row["importance"]),
            confidence=float(row["confidence"]),
            useful_score=float(row["useful_score"]),
            useful_count=int(row["useful_count"]),
            invalid_count=int(row["invalid_count"]),
            group_safe=bool(row["group_safe"]),
            index_status=IndexStatus(str(row["index_status"])),
            index_error=row["index_error"],
            content=str(row["revision_content"]),
            structured_payload=self._json_object(row["revision_structured_payload"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def _access_clause(
        self, context: MemoryAccessContext, alias: str = "mi"
    ) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = [context.owner_user_id]
        if MemoryScope.USER in context.allowed_scopes:
            if context.is_group:
                clauses.append(f"({alias}.scope = 'user' AND {alias}.group_safe = 1)")
            else:
                clauses.append(f"{alias}.scope = 'user'")
        if MemoryScope.PERSONA in context.allowed_scopes and context.persona_id:
            group_filter = f" AND {alias}.group_safe = 1" if context.is_group else ""
            clauses.append(f"({alias}.scope = 'persona' AND {alias}.persona_id = ?{group_filter})")
            params.append(context.persona_id)
        if MemoryScope.SESSION in context.allowed_scopes:
            clauses.append(f"({alias}.scope = 'session' AND {alias}.session_id = ?)")
            params.append(context.session_id)
        if MemoryScope.LEGACY_SESSION in context.allowed_scopes and context.allow_legacy_session:
            clauses.append(f"({alias}.scope = 'legacy_session' AND {alias}.session_id = ?)")
            params.append(context.session_id)
        if MemoryScope.PUBLIC in context.allowed_scopes and context.allow_public:
            clauses.append(f"{alias}.scope = 'public'")
        if not clauses:
            return "1 = 0", []
        return f"{alias}.owner_user_id = ? AND ({' OR '.join(clauses)})", params

    async def list_items(
        self,
        *,
        context: MemoryAccessContext,
        statuses: Iterable[MemoryItemStatus] | None = None,
        scopes: Iterable[MemoryScope] | None = None,
        item_type: str | None = None,
        sort_by: str = "updated_at",
        sort_direction: str = "desc",
        limit: int = 50,
        offset: int = 0,
    ) -> list[MemoryItem]:
        if sort_by not in self._SORT_COLUMNS:
            raise ValueError("不允许的排序字段")
        direction = self._SORT_DIRECTIONS.get(sort_direction.casefold())
        if direction is None:
            raise ValueError("不允许的排序方向")
        where, params = self._access_clause(context)
        filters = [where]
        normalized_statuses = tuple(statuses or ())
        if normalized_statuses:
            placeholders = ",".join("?" for _ in normalized_statuses)
            filters.append(f"mi.status IN ({placeholders})")
            params.extend(status.value for status in normalized_statuses)
        normalized_scopes = tuple(scopes or ())
        if normalized_scopes:
            placeholders = ",".join("?" for _ in normalized_scopes)
            filters.append(f"mi.scope IN ({placeholders})")
            params.extend(scope.value for scope in normalized_scopes)
        if item_type:
            filters.append("mi.item_type = ?")
            params.append(item_type)
        params.extend((max(1, min(int(limit), 500)), max(0, int(offset))))
        async with self._connect() as db:
            cursor = await db.execute(
                f"""
                SELECT mi.*, r.content AS revision_content,
                       r.structured_payload AS revision_structured_payload
                FROM memory_items mi
                JOIN memory_item_revisions r
                  ON r.memory_item_id = mi.memory_item_id
                 AND r.revision_no = mi.current_revision_no
                WHERE {' AND '.join(filters)}
                ORDER BY {self._SORT_COLUMNS[sort_by]} {direction}, mi.memory_item_id ASC
                LIMIT ? OFFSET ?
                """,
                params,
            )
            rows = await cursor.fetchall()
        return [self._row_to_item(row) for row in rows]

    async def list_items_for_owner(
        self,
        *,
        owner_user_id: str,
        keyword: str | None = None,
        scope: MemoryScope | None = None,
        persona_id: str | None = None,
        status: MemoryItemStatus | None = None,
        item_type: str | None = None,
        conflict: bool | None = None,
        index_status: IndexStatus | None = None,
        sort_by: str = "updated_at",
        sort_direction: str = "desc",
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[MemoryItem], int]:
        if await self.get_owner(owner_user_id) is None:
            raise EvolvingMemoryNotFoundError("owner 不存在")
        if sort_by not in self._SORT_COLUMNS:
            raise ValueError("不允许的排序字段")
        direction = self._SORT_DIRECTIONS.get(sort_direction.casefold())
        if direction is None:
            raise ValueError("不允许的排序方向")
        filters = ["mi.owner_user_id = ?"]
        params: list[Any] = [owner_user_id]
        normalized_keyword = str(keyword or "").strip()
        if normalized_keyword:
            filters.append(
                "(mi.memory_item_id LIKE ? COLLATE NOCASE OR mi.canonical_key LIKE ? COLLATE NOCASE OR r.content LIKE ? COLLATE NOCASE)"
            )
            pattern = f"%{normalized_keyword}%"
            params.extend((pattern, pattern, pattern))
        if scope is not None:
            filters.append("mi.scope = ?")
            params.append(scope.value)
        if persona_id is not None:
            filters.append("mi.persona_id = ?")
            params.append(persona_id)
        if status is not None:
            filters.append("mi.status = ?")
            params.append(status.value)
        if item_type:
            filters.append("mi.item_type = ?")
            params.append(item_type)
        if conflict is not None:
            operator = "EXISTS" if conflict else "NOT EXISTS"
            filters.append(
                f"{operator} (SELECT 1 FROM memory_conflicts mc WHERE mc.owner_user_id = mi.owner_user_id AND mc.status = 'open' AND (mc.left_item_id = mi.memory_item_id OR mc.right_item_id = mi.memory_item_id))"
            )
        if index_status is not None:
            filters.append("mi.index_status = ?")
            params.append(index_status.value)
        where_clause = " AND ".join(filters)
        effective_limit = max(1, min(int(limit), 500))
        effective_offset = max(0, int(offset))
        async with self._connect() as db:
            count_cursor = await db.execute(
                f"""
                SELECT COUNT(*)
                FROM memory_items mi
                JOIN memory_item_revisions r
                  ON r.memory_item_id = mi.memory_item_id
                 AND r.revision_no = mi.current_revision_no
                WHERE {where_clause}
                """,
                params,
            )
            count_row = await count_cursor.fetchone()
            cursor = await db.execute(
                f"""
                SELECT mi.*, r.content AS revision_content,
                       r.structured_payload AS revision_structured_payload
                FROM memory_items mi
                JOIN memory_item_revisions r
                  ON r.memory_item_id = mi.memory_item_id
                 AND r.revision_no = mi.current_revision_no
                WHERE {where_clause}
                ORDER BY {self._SORT_COLUMNS[sort_by]} {direction}, mi.memory_item_id ASC
                LIMIT ? OFFSET ?
                """,
                (*params, effective_limit, effective_offset),
            )
            rows = await cursor.fetchall()
        return [self._row_to_item(row) for row in rows], int(count_row[0] if count_row else 0)

    async def get_item_admin_counts(
        self,
        *,
        owner_user_id: str,
        memory_item_ids: Iterable[str],
    ) -> dict[str, dict[str, int]]:
        item_ids = tuple(dict.fromkeys(str(item_id) for item_id in memory_item_ids if item_id))
        if not item_ids:
            return {}
        placeholders = ",".join("?" for _ in item_ids)
        counts = {
            item_id: {"conflict_count": 0, "source_count": 0, "relation_count": 0}
            for item_id in item_ids
        }
        async with self._connect() as db:
            cursor = await db.execute(
                f"""
                SELECT left_item_id, right_item_id
                FROM memory_conflicts
                WHERE owner_user_id = ? AND status = 'open'
                  AND (left_item_id IN ({placeholders}) OR right_item_id IN ({placeholders}))
                """,
                (owner_user_id, *item_ids, *item_ids),
            )
            for row in await cursor.fetchall():
                for item_id in {str(row["left_item_id"]), str(row["right_item_id"])}:
                    if item_id in counts:
                        counts[item_id]["conflict_count"] += 1
            cursor = await db.execute(
                f"""
                SELECT memory_item_id, COUNT(*) AS total
                FROM memory_item_sources
                WHERE owner_user_id = ? AND memory_item_id IN ({placeholders})
                GROUP BY memory_item_id
                """,
                (owner_user_id, *item_ids),
            )
            for row in await cursor.fetchall():
                counts[str(row["memory_item_id"])]["source_count"] = int(row["total"])
            cursor = await db.execute(
                f"""
                SELECT source_item_id, target_item_id
                FROM memory_item_relations
                WHERE owner_user_id = ?
                  AND (source_item_id IN ({placeholders}) OR target_item_id IN ({placeholders}))
                """,
                (owner_user_id, *item_ids, *item_ids),
            )
            for row in await cursor.fetchall():
                for item_id in {str(row["source_item_id"]), str(row["target_item_id"])}:
                    if item_id in counts:
                        counts[item_id]["relation_count"] += 1
        return counts

    async def get_item_by_document_id(
        self,
        *,
        current_document_id: int,
    ) -> MemoryItem | None:
        async with self._connect() as db:
            cursor = await db.execute(
                """
                SELECT mi.*, r.content AS revision_content,
                       r.structured_payload AS revision_structured_payload
                FROM memory_items mi
                JOIN memory_item_revisions r
                  ON r.memory_item_id = mi.memory_item_id
                 AND r.revision_no = mi.current_revision_no
                WHERE mi.current_document_id = ?
                ORDER BY mi.updated_at DESC
                LIMIT 1
                """,
                (int(current_document_id),),
            )
            row = await cursor.fetchone()
        return self._row_to_item(row) if row else None

    async def admin_update_item(
        self,
        *,
        context: MemoryAccessContext,
        target_context: MemoryAccessContext,
        memory_item_id: str,
        expected_version: int,
        operation_key: str,
        actor_id: str,
        content: str | None = None,
        canonical_key: str | None = None,
        structured_payload: dict[str, Any] | None = None,
        item_type: str | None = None,
        importance: float | None = None,
        confidence: float | None = None,
        group_safe: bool | None = None,
        status: MemoryItemStatus | None = None,
        scope: MemoryScope | None = None,
        session_id: str | None = None,
        persona_id: str | None = None,
        new_owner_user_id: str | None = None,
        reason: str | None = None,
        source: dict[str, Any] | None = None,
    ) -> tuple[MemoryItem, bool]:
        operation_request = {
            "memory_item_id": memory_item_id,
            "expected_version": expected_version,
            "target_owner_user_id": target_context.owner_user_id,
            "target_session_id": target_context.session_id,
            "target_persona_id": target_context.persona_id,
            "content": content,
            "canonical_key": canonical_key,
            "structured_payload": structured_payload,
            "item_type": item_type,
            "importance": importance,
            "confidence": confidence,
            "group_safe": group_safe,
            "status": status.value if status else None,
            "scope": scope.value if scope else None,
            "session_id": session_id,
            "persona_id": persona_id,
            "new_owner_user_id": new_owner_user_id,
            "actor_id": actor_id,
            "reason": reason,
            "source": source,
        }
        async with self._write_transaction(context.owner_user_id, memory_item_id) as db:
            replay = await self._load_idempotent_item(
                db,
                operation_key=operation_key,
                owner_user_id=context.owner_user_id,
                action=MemoryAction.UPDATE,
                request_payload=operation_request,
            )
            if replay is not None:
                return replay, True
            current = await self._require_accessible_item_db(db, context, memory_item_id)
            self._assert_version(current, expected_version)
            target_owner_id = (new_owner_user_id or current.owner_user_id).strip()
            if target_context.owner_user_id != target_owner_id:
                raise EvolvingMemoryAccessError("目标 access context 与 owner 不一致")
            target_owner = await self._get_owner_db(db, target_owner_id)
            if target_owner is None:
                raise EvolvingMemoryNotFoundError("目标 owner 不存在")
            if target_owner.status != OwnerStatus.ACTIVE:
                raise EvolvingMemoryAccessError("目标 owner 当前不可写")
            if target_owner_id != context.owner_user_id:
                relation_cursor = await db.execute(
                    """
                    SELECT 1 FROM memory_item_relations
                    WHERE owner_user_id = ? AND (source_item_id = ? OR target_item_id = ?)
                    LIMIT 1
                    """,
                    (context.owner_user_id, memory_item_id, memory_item_id),
                )
                conflict_cursor = await db.execute(
                    """
                    SELECT 1 FROM memory_conflicts
                    WHERE owner_user_id = ? AND (left_item_id = ? OR right_item_id = ?)
                    LIMIT 1
                    """,
                    (context.owner_user_id, memory_item_id, memory_item_id),
                )
                if await relation_cursor.fetchone() is not None or await conflict_cursor.fetchone() is not None:
                    raise EvolvingMemoryAccessError(
                        "存在关系或冲突的对象不能单独跨 owner 移动，请先处理关联对象"
                    )
            new_scope = scope or current.scope
            new_session_id = session_id if new_scope in {MemoryScope.SESSION, MemoryScope.LEGACY_SESSION} else None
            if new_scope in {MemoryScope.SESSION, MemoryScope.LEGACY_SESSION} and new_session_id is None:
                new_session_id = current.session_id if current.scope == new_scope else None
            new_persona_id = persona_id if new_scope == MemoryScope.PERSONA else None
            if new_scope == MemoryScope.PERSONA and new_persona_id is None:
                new_persona_id = current.persona_id if current.scope == MemoryScope.PERSONA else None
            new_group_safe = current.group_safe if group_safe is None else bool(group_safe)
            self._validate_scope_access(
                target_context,
                new_scope,
                new_session_id,
                new_persona_id,
                new_group_safe,
            )
            new_content = content.strip() if content is not None else current.content
            new_canonical = canonical_key.strip() if canonical_key is not None else current.canonical_key
            if not new_content or not new_canonical:
                raise ValueError("content 和 canonical_key 不得为空")
            new_item_type = item_type.strip() if item_type is not None else current.item_type
            if not new_item_type:
                raise ValueError("item_type 不得为空")
            new_status = status or current.status
            new_revision_no = current.current_revision_no + 1
            digest = self.content_hash(new_content)
            now = utc_now_iso()
            await db.execute(
                """
                INSERT INTO memory_item_revisions(
                    revision_id, memory_item_id, owner_user_id, revision_no,
                    operation, content, content_hash, structured_payload,
                    base_version, actor_type, actor_id, reason, created_at
                ) VALUES (?, ?, ?, ?, 'update', ?, ?, ?, ?, 'admin', ?, ?, ?)
                """,
                (
                    self._new_id("rev"),
                    memory_item_id,
                    target_owner_id,
                    new_revision_no,
                    new_content,
                    digest,
                    self._json_dump(
                        structured_payload
                        if structured_payload is not None
                        else current.structured_payload
                    ),
                    expected_version,
                    actor_id,
                    reason,
                    now,
                ),
            )
            cursor = await db.execute(
                """
                UPDATE memory_items
                SET owner_user_id = ?, scope = ?, session_id = ?, persona_id = ?,
                    item_type = ?, canonical_key = ?, content_hash = ?, status = ?,
                    current_revision_no = ?, version = version + 1,
                    importance = ?, confidence = ?, group_safe = ?,
                    index_status = 'pending', index_error = NULL, updated_at = ?
                WHERE owner_user_id = ? AND memory_item_id = ? AND version = ?
                """,
                (
                    target_owner_id,
                    new_scope.value,
                    new_session_id,
                    new_persona_id,
                    new_item_type,
                    new_canonical,
                    digest,
                    new_status.value,
                    new_revision_no,
                    float(current.importance if importance is None else importance),
                    float(current.confidence if confidence is None else confidence),
                    int(new_group_safe),
                    now,
                    context.owner_user_id,
                    memory_item_id,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                latest = await self._get_item_db(db, context.owner_user_id, memory_item_id)
                raise EvolvingMemoryVersionConflictError(
                    memory_item_id,
                    expected_version,
                    latest.version if latest else -1,
                )
            if target_owner_id != context.owner_user_id:
                for table_name in (
                    "memory_item_sources",
                    "memory_item_relations",
                    "memory_conflicts",
                ):
                    await db.execute(
                        f"UPDATE {table_name} SET owner_user_id = ? WHERE owner_user_id = ? AND "
                        + (
                            "memory_item_id = ?"
                            if table_name == "memory_item_sources"
                            else "(source_item_id = ? OR target_item_id = ?)"
                            if table_name == "memory_item_relations"
                            else "(left_item_id = ? OR right_item_id = ?)"
                        ),
                        (
                            (target_owner_id, context.owner_user_id, memory_item_id)
                            if table_name == "memory_item_sources"
                            else (
                                target_owner_id,
                                context.owner_user_id,
                                memory_item_id,
                                memory_item_id,
                            )
                        ),
                    )
            await self._refresh_fts_db(
                db,
                memory_item_id=memory_item_id,
                owner_user_id=target_owner_id,
                status=new_status,
                content=new_content,
                canonical_key=new_canonical,
                item_type=new_item_type,
            )
            if source:
                await self._insert_source_db(
                    db,
                    owner_user_id=target_owner_id,
                    memory_item_id=memory_item_id,
                    revision_no=new_revision_no,
                    source=source,
                )
            await self._record_operation_db(
                db,
                operation_key=operation_key,
                action=MemoryAction.UPDATE,
                owner_user_id=context.owner_user_id,
                entity_id=memory_item_id,
                version=expected_version + 1,
                request_payload=operation_request,
                extra={"target_owner_user_id": target_owner_id},
            )
            updated = await self._get_item_db(db, target_owner_id, memory_item_id)
            if updated is None:
                raise EvolvingMemoryNotFoundError()
            return updated, False

    async def update_item(
        self,
        *,
        context: MemoryAccessContext,
        memory_item_id: str,
        expected_version: int,
        operation_key: str,
        actor_type: MemoryActorType,
        actor_id: str,
        content: str | None = None,
        canonical_key: str | None = None,
        structured_payload: dict[str, Any] | None = None,
        item_type: str | None = None,
        importance: float | None = None,
        confidence: float | None = None,
        group_safe: bool | None = None,
        status: MemoryItemStatus | None = None,
        reason: str | None = None,
        source: dict[str, Any] | None = None,
    ) -> tuple[MemoryItem, bool]:
        operation_request = {
            "memory_item_id": memory_item_id,
            "expected_version": expected_version,
            "content": content,
            "canonical_key": canonical_key,
            "structured_payload": structured_payload,
            "item_type": item_type,
            "importance": importance,
            "confidence": confidence,
            "group_safe": group_safe,
            "status": status.value if status else None,
            "actor_type": actor_type.value,
            "actor_id": actor_id,
            "reason": reason,
            "source": source,
        }
        async with self._write_transaction(context.owner_user_id, memory_item_id) as db:
            replay = await self._load_idempotent_item(
                db,
                operation_key=operation_key,
                owner_user_id=context.owner_user_id,
                action=MemoryAction.UPDATE,
                request_payload=operation_request,
            )
            if replay is not None:
                return replay, True
            current = await self._require_accessible_item_db(db, context, memory_item_id)
            self._assert_version(current, expected_version)
            new_content = (content.strip() if content is not None else current.content)
            new_canonical = (
                canonical_key.strip() if canonical_key is not None else current.canonical_key
            )
            if not new_content or not new_canonical:
                raise ValueError("content 和 canonical_key 不得为空")
            new_item_type = item_type or current.item_type
            new_group_safe = current.group_safe if group_safe is None else bool(group_safe)
            self._validate_scope_access(
                context,
                current.scope,
                current.session_id,
                current.persona_id,
                new_group_safe,
            )
            new_status = status or current.status
            new_revision_no = current.current_revision_no + 1
            now = utc_now_iso()
            digest = self.content_hash(new_content)
            await self._insert_revision_db(
                db,
                item=current,
                revision_no=new_revision_no,
                operation=RevisionOperation.UPDATE,
                content=new_content,
                content_hash=digest,
                structured_payload=(
                    structured_payload
                    if structured_payload is not None
                    else current.structured_payload
                ),
                base_version=expected_version,
                actor_type=actor_type,
                actor_id=actor_id,
                reason=reason,
            )
            cursor = await db.execute(
                """
                UPDATE memory_items
                SET canonical_key = ?, content_hash = ?, item_type = ?, status = ?,
                    current_revision_no = ?, version = version + 1,
                    importance = ?, confidence = ?, group_safe = ?,
                    index_status = 'pending', index_error = NULL, updated_at = ?
                WHERE owner_user_id = ? AND memory_item_id = ? AND version = ?
                """,
                (
                    new_canonical,
                    digest,
                    new_item_type,
                    new_status.value,
                    new_revision_no,
                    float(current.importance if importance is None else importance),
                    float(current.confidence if confidence is None else confidence),
                    int(new_group_safe),
                    now,
                    context.owner_user_id,
                    memory_item_id,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                latest = await self._get_item_db(db, context.owner_user_id, memory_item_id)
                raise EvolvingMemoryVersionConflictError(
                    memory_item_id,
                    expected_version,
                    latest.version if latest else -1,
                )
            await self._refresh_fts_db(
                db,
                memory_item_id=memory_item_id,
                owner_user_id=context.owner_user_id,
                status=new_status,
                content=new_content,
                canonical_key=new_canonical,
                item_type=new_item_type,
            )
            if source:
                await self._insert_source_db(
                    db,
                    owner_user_id=context.owner_user_id,
                    memory_item_id=memory_item_id,
                    revision_no=new_revision_no,
                    source=source,
                )
            await self._record_operation_db(
                db,
                operation_key=operation_key,
                action=MemoryAction.UPDATE,
                owner_user_id=context.owner_user_id,
                entity_id=memory_item_id,
                version=expected_version + 1,
                request_payload=operation_request,
            )
            updated = await self._get_item_db(db, context.owner_user_id, memory_item_id)
            if updated is None:
                raise EvolvingMemoryNotFoundError()
            return updated, False

    async def merge_items(
        self,
        *,
        context: MemoryAccessContext,
        survivor_item_id: str,
        source_item_ids: list[str],
        expected_versions: dict[str, int],
        content: str,
        canonical_key: str,
        operation_key: str,
        actor_type: MemoryActorType,
        actor_id: str,
        reason: str | None = None,
        structured_payload: dict[str, Any] | None = None,
    ) -> tuple[MemoryItem, bool]:
        normalized_sources = sorted({item for item in source_item_ids if item != survivor_item_id})
        if not normalized_sources:
            raise ValueError("merge 至少需要一个来源对象")
        operation_request = {
            "survivor_item_id": survivor_item_id,
            "source_item_ids": normalized_sources,
            "expected_versions": expected_versions,
            "content": content,
            "canonical_key": canonical_key,
            "structured_payload": structured_payload,
            "actor_type": actor_type.value,
            "actor_id": actor_id,
            "reason": reason,
        }
        lock_key = "merge:" + ":".join(sorted([survivor_item_id, *normalized_sources]))
        async with self._write_transaction(context.owner_user_id, lock_key) as db:
            replay = await self._load_idempotent_item(
                db,
                operation_key=operation_key,
                owner_user_id=context.owner_user_id,
                action=MemoryAction.MERGE,
                request_payload=operation_request,
            )
            if replay is not None:
                return replay, True
            survivor = await self._require_accessible_item_db(db, context, survivor_item_id)
            all_items = {survivor_item_id: survivor}
            for item_id in normalized_sources:
                all_items[item_id] = await self._require_accessible_item_db(db, context, item_id)
            for item_id, item in all_items.items():
                if item_id not in expected_versions:
                    raise ValueError(f"缺少 expected_version: {item_id}")
                self._assert_version(item, expected_versions[item_id])
            now = utc_now_iso()
            merged_content = content.strip()
            merged_key = canonical_key.strip()
            if not merged_content or not merged_key:
                raise ValueError("merge content 和 canonical_key 不得为空")
            survivor_revision = survivor.current_revision_no + 1
            survivor_hash = self.content_hash(merged_content)
            await self._insert_revision_db(
                db,
                item=survivor,
                revision_no=survivor_revision,
                operation=RevisionOperation.MERGE,
                content=merged_content,
                content_hash=survivor_hash,
                structured_payload=structured_payload or survivor.structured_payload,
                base_version=survivor.version,
                actor_type=actor_type,
                actor_id=actor_id,
                reason=reason,
            )
            await db.execute(
                """
                UPDATE memory_items
                SET canonical_key = ?, content_hash = ?, current_revision_no = ?,
                    version = version + 1, status = 'active', index_status = 'pending',
                    index_error = NULL, updated_at = ?
                WHERE owner_user_id = ? AND memory_item_id = ? AND version = ?
                """,
                (
                    merged_key,
                    survivor_hash,
                    survivor_revision,
                    now,
                    context.owner_user_id,
                    survivor_item_id,
                    survivor.version,
                ),
            )
            await self._refresh_fts_db(
                db,
                memory_item_id=survivor_item_id,
                owner_user_id=context.owner_user_id,
                status=MemoryItemStatus.ACTIVE,
                content=merged_content,
                canonical_key=merged_key,
                item_type=survivor.item_type,
            )
            for source_id in normalized_sources:
                source_item = all_items[source_id]
                source_revision = source_item.current_revision_no + 1
                await self._insert_revision_db(
                    db,
                    item=source_item,
                    revision_no=source_revision,
                    operation=RevisionOperation.MERGE,
                    content=source_item.content,
                    content_hash=source_item.content_hash,
                    structured_payload=source_item.structured_payload,
                    base_version=source_item.version,
                    actor_type=actor_type,
                    actor_id=actor_id,
                    reason=reason,
                )
                await db.execute(
                    """
                    UPDATE memory_items
                    SET status = 'superseded', current_revision_no = ?,
                        version = version + 1, index_status = 'pending',
                        index_error = NULL, updated_at = ?
                    WHERE owner_user_id = ? AND memory_item_id = ? AND version = ?
                    """,
                    (
                        source_revision,
                        now,
                        context.owner_user_id,
                        source_id,
                        source_item.version,
                    ),
                )
                await self._refresh_fts_db(
                    db,
                    memory_item_id=source_id,
                    owner_user_id=context.owner_user_id,
                    status=MemoryItemStatus.SUPERSEDED,
                    content=source_item.content,
                    canonical_key=source_item.canonical_key,
                    item_type=source_item.item_type,
                )
                await self._insert_relation_db(
                    db,
                    owner_user_id=context.owner_user_id,
                    source_item_id=source_id,
                    target_item_id=survivor_item_id,
                    relation_type=MemoryRelationType.MERGED_INTO,
                    source_revision_no=source_revision,
                    metadata={"operation_key": operation_key},
                )
            await self._record_operation_db(
                db,
                operation_key=operation_key,
                action=MemoryAction.MERGE,
                owner_user_id=context.owner_user_id,
                entity_id=survivor_item_id,
                version=survivor.version + 1,
                request_payload=operation_request,
                extra={"affected_item_ids": normalized_sources},
            )
            updated = await self._get_item_db(db, context.owner_user_id, survivor_item_id)
            if updated is None:
                raise EvolvingMemoryNotFoundError()
            return updated, False

    async def supersede_item(
        self,
        *,
        context: MemoryAccessContext,
        old_item_id: str,
        replacement_item_id: str,
        expected_versions: dict[str, int],
        operation_key: str,
        actor_type: MemoryActorType,
        actor_id: str,
        reason: str | None = None,
    ) -> tuple[MemoryItem, bool]:
        if old_item_id == replacement_item_id:
            raise ValueError("replacement_item_id 不得与 old_item_id 相同")
        operation_request = {
            "old_item_id": old_item_id,
            "replacement_item_id": replacement_item_id,
            "expected_versions": expected_versions,
            "actor_type": actor_type.value,
            "actor_id": actor_id,
            "reason": reason,
        }
        lock_key = "supersede:" + ":".join(sorted((old_item_id, replacement_item_id)))
        async with self._write_transaction(context.owner_user_id, lock_key) as db:
            replay = await self._load_idempotent_item(
                db,
                operation_key=operation_key,
                owner_user_id=context.owner_user_id,
                action=MemoryAction.SUPERSEDE,
                request_payload=operation_request,
            )
            if replay is not None:
                return replay, True
            old_item = await self._require_accessible_item_db(db, context, old_item_id)
            replacement = await self._require_accessible_item_db(db, context, replacement_item_id)
            for item in (old_item, replacement):
                expected = expected_versions.get(item.memory_item_id)
                if expected is None:
                    raise ValueError(f"缺少 expected_version: {item.memory_item_id}")
                self._assert_version(item, expected)
            now = utc_now_iso()
            for item, new_status in (
                (old_item, MemoryItemStatus.SUPERSEDED),
                (replacement, MemoryItemStatus.ACTIVE),
            ):
                revision_no = item.current_revision_no + 1
                await self._insert_revision_db(
                    db,
                    item=item,
                    revision_no=revision_no,
                    operation=RevisionOperation.SUPERSEDE,
                    content=item.content,
                    content_hash=item.content_hash,
                    structured_payload=item.structured_payload,
                    base_version=item.version,
                    actor_type=actor_type,
                    actor_id=actor_id,
                    reason=reason,
                )
                await db.execute(
                    """
                    UPDATE memory_items
                    SET status = ?, current_revision_no = ?, version = version + 1,
                        index_status = 'pending', index_error = NULL, updated_at = ?
                    WHERE owner_user_id = ? AND memory_item_id = ? AND version = ?
                    """,
                    (
                        new_status.value,
                        revision_no,
                        now,
                        context.owner_user_id,
                        item.memory_item_id,
                        item.version,
                    ),
                )
                await self._refresh_fts_db(
                    db,
                    memory_item_id=item.memory_item_id,
                    owner_user_id=context.owner_user_id,
                    status=new_status,
                    content=item.content,
                    canonical_key=item.canonical_key,
                    item_type=item.item_type,
                )
            await self._insert_relation_db(
                db,
                owner_user_id=context.owner_user_id,
                source_item_id=replacement_item_id,
                target_item_id=old_item_id,
                relation_type=MemoryRelationType.SUPERSEDES,
                source_revision_no=replacement.current_revision_no + 1,
                metadata={"operation_key": operation_key},
            )
            await self._record_operation_db(
                db,
                operation_key=operation_key,
                action=MemoryAction.SUPERSEDE,
                owner_user_id=context.owner_user_id,
                entity_id=replacement_item_id,
                version=replacement.version + 1,
                request_payload=operation_request,
                extra={"affected_item_ids": [old_item_id]},
            )
            updated = await self._get_item_db(db, context.owner_user_id, replacement_item_id)
            if updated is None:
                raise EvolvingMemoryNotFoundError()
            return updated, False

    async def archive_item(
        self,
        *,
        context: MemoryAccessContext,
        memory_item_id: str,
        expected_version: int,
        operation_key: str,
        actor_type: MemoryActorType,
        actor_id: str,
        reason: str | None = None,
    ) -> tuple[MemoryItem, bool]:
        operation_request = {
            "memory_item_id": memory_item_id,
            "expected_version": expected_version,
            "actor_type": actor_type.value,
            "actor_id": actor_id,
            "reason": reason,
        }
        async with self._write_transaction(context.owner_user_id, memory_item_id) as db:
            replay = await self._load_idempotent_item(
                db,
                operation_key=operation_key,
                owner_user_id=context.owner_user_id,
                action=MemoryAction.ARCHIVE,
                request_payload=operation_request,
            )
            if replay is not None:
                return replay, True
            current = await self._require_accessible_item_db(db, context, memory_item_id)
            self._assert_version(current, expected_version)
            revision_no = current.current_revision_no + 1
            await self._insert_revision_db(
                db,
                item=current,
                revision_no=revision_no,
                operation=RevisionOperation.ARCHIVE,
                content=current.content,
                content_hash=current.content_hash,
                structured_payload=current.structured_payload,
                base_version=current.version,
                actor_type=actor_type,
                actor_id=actor_id,
                reason=reason,
            )
            await db.execute(
                """
                UPDATE memory_items
                SET status = 'archived', current_revision_no = ?, version = version + 1,
                    index_status = 'pending', index_error = NULL, updated_at = ?
                WHERE owner_user_id = ? AND memory_item_id = ? AND version = ?
                """,
                (
                    revision_no,
                    utc_now_iso(),
                    context.owner_user_id,
                    memory_item_id,
                    expected_version,
                ),
            )
            await self._refresh_fts_db(
                db,
                memory_item_id=memory_item_id,
                owner_user_id=context.owner_user_id,
                status=MemoryItemStatus.ARCHIVED,
                content=current.content,
                canonical_key=current.canonical_key,
                item_type=current.item_type,
            )
            await self._record_operation_db(
                db,
                operation_key=operation_key,
                action=MemoryAction.ARCHIVE,
                owner_user_id=context.owner_user_id,
                entity_id=memory_item_id,
                version=expected_version + 1,
                request_payload=operation_request,
            )
            updated = await self._get_item_db(db, context.owner_user_id, memory_item_id)
            if updated is None:
                raise EvolvingMemoryNotFoundError()
            return updated, False

    async def record_feedback(
        self,
        *,
        context: MemoryAccessContext,
        memory_item_id: str,
        expected_version: int,
        useful: bool,
        score_delta: float,
        operation_key: str,
        actor_type: MemoryActorType,
        actor_id: str,
        reason: str | None = None,
    ) -> tuple[MemoryItem, bool]:
        operation_request = {
            "memory_item_id": memory_item_id,
            "expected_version": expected_version,
            "useful": bool(useful),
            "score_delta": float(score_delta),
            "actor_type": actor_type.value,
            "actor_id": actor_id,
            "reason": reason,
        }
        async with self._write_transaction(context.owner_user_id, memory_item_id) as db:
            replay = await self._load_idempotent_item(
                db,
                operation_key=operation_key,
                owner_user_id=context.owner_user_id,
                action=None,
                request_payload=operation_request,
                op_type="feedback",
            )
            if replay is not None:
                return replay, True
            current = await self._require_accessible_item_db(db, context, memory_item_id)
            self._assert_version(current, expected_version)
            signed_delta = abs(float(score_delta)) if useful else -abs(float(score_delta))
            new_score = max(-1.0, min(1.0, current.useful_score + signed_delta))
            await db.execute(
                """
                UPDATE memory_items
                SET useful_score = ?,
                    useful_count = useful_count + ?,
                    invalid_count = invalid_count + ?,
                    version = version + 1,
                    updated_at = ?
                WHERE owner_user_id = ? AND memory_item_id = ? AND version = ?
                """,
                (
                    new_score,
                    int(useful),
                    int(not useful),
                    utc_now_iso(),
                    context.owner_user_id,
                    memory_item_id,
                    expected_version,
                ),
            )
            await self._record_operation_db(
                db,
                operation_key=operation_key,
                action=None,
                owner_user_id=context.owner_user_id,
                entity_id=memory_item_id,
                version=expected_version + 1,
                request_payload=operation_request,
                op_type="feedback",
                extra={
                    "useful": useful,
                    "actor_type": actor_type.value,
                    "actor_id": actor_id,
                    "reason": reason,
                },
            )
            updated = await self._get_item_db(db, context.owner_user_id, memory_item_id)
            if updated is None:
                raise EvolvingMemoryNotFoundError()
            return updated, False

    async def _insert_revision_db(
        self,
        db: aiosqlite.Connection,
        *,
        item: MemoryItem,
        revision_no: int,
        operation: RevisionOperation,
        content: str,
        content_hash: str,
        structured_payload: dict[str, Any],
        base_version: int,
        actor_type: MemoryActorType,
        actor_id: str,
        reason: str | None,
    ) -> None:
        await db.execute(
            """
            INSERT INTO memory_item_revisions(
                revision_id, memory_item_id, owner_user_id, revision_no,
                operation, content, content_hash, structured_payload,
                base_version, actor_type, actor_id, reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self._new_id("rev"),
                item.memory_item_id,
                item.owner_user_id,
                revision_no,
                operation.value,
                content,
                content_hash,
                self._json_dump(structured_payload),
                base_version,
                actor_type.value,
                actor_id,
                reason,
                utc_now_iso(),
            ),
        )

    def _assert_version(self, item: MemoryItem, expected_version: int) -> None:
        if item.version != expected_version:
            raise EvolvingMemoryVersionConflictError(
                item.memory_item_id, expected_version, item.version
            )

    async def _require_accessible_item_db(
        self,
        db: aiosqlite.Connection,
        context: MemoryAccessContext,
        memory_item_id: str,
    ) -> MemoryItem:
        item = await self._get_item_db(db, context.owner_user_id, memory_item_id)
        if item is None:
            raise EvolvingMemoryNotFoundError()
        if not context.can_access_item(item):
            raise EvolvingMemoryAccessError()
        return item

    async def _refresh_fts_db(
        self,
        db: aiosqlite.Connection,
        *,
        memory_item_id: str,
        owner_user_id: str,
        status: MemoryItemStatus,
        content: str,
        canonical_key: str,
        item_type: str,
    ) -> None:
        await db.execute(
            "DELETE FROM livingmemory_memory_items_fts WHERE memory_item_id = ?",
            (memory_item_id,),
        )
        if status in {MemoryItemStatus.ACTIVE, MemoryItemStatus.CONFLICTED}:
            await db.execute(
                """
                INSERT INTO livingmemory_memory_items_fts(
                    content, canonical_key, memory_item_id, owner_user_id, item_type
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (content, canonical_key, memory_item_id, owner_user_id, item_type),
            )

    async def _load_operation_db(
        self,
        db: aiosqlite.Connection,
        *,
        operation_key: str,
        owner_user_id: str,
        operation_type: str,
        request_payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        cursor = await db.execute(
            """
            SELECT op_type, status, payload, entity_id
            FROM memory_write_ops
            WHERE operation_key = ?
            """,
            (operation_key,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        payload = self._json_object(row["payload"])
        normalized_request, request_digest = self._operation_request_digest(request_payload)
        if (
            payload.get("owner_user_id") != owner_user_id
            or str(row["op_type"]) != operation_type
            or str(row["status"]) != "completed"
            or payload.get("request_digest") != request_digest
            or payload.get("request") != normalized_request
        ):
            raise EvolvingMemoryIdempotencyError(
                "operation_key 的 owner、operation 或 payload 与原请求不一致"
            )
        return payload

    async def _load_idempotent_item(
        self,
        db: aiosqlite.Connection,
        *,
        operation_key: str,
        owner_user_id: str,
        action: MemoryAction | None,
        request_payload: dict[str, Any],
        op_type: str | None = None,
    ) -> MemoryItem | None:
        operation_type = op_type or (action.value if action else "feedback")
        payload = await self._load_operation_db(
            db,
            operation_key=operation_key,
            owner_user_id=owner_user_id,
            operation_type=operation_type,
            request_payload=request_payload,
        )
        if payload is None:
            return None
        entity_id = str(payload.get("entity_id") or "")
        if not entity_id:
            raise EvolvingMemoryIdempotencyError("幂等记录缺少 entity_id")
        item = await self._get_item_db(db, owner_user_id, entity_id)
        if item is None:
            raise EvolvingMemoryIdempotencyError("幂等记录引用的对象不存在")
        return item

    async def _record_operation_db(
        self,
        db: aiosqlite.Connection,
        *,
        operation_key: str,
        action: MemoryAction | None,
        owner_user_id: str,
        entity_id: str,
        version: int,
        request_payload: dict[str, Any],
        op_type: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        now = time.time()
        operation_type = op_type or (action.value if action else "feedback")
        normalized_request, request_digest = self._operation_request_digest(request_payload)
        payload = {
            "owner_user_id": owner_user_id,
            "entity_id": entity_id,
            "version": version,
            "request": normalized_request,
            "request_digest": request_digest,
            **(extra or {}),
        }
        await db.execute(
            """
            INSERT INTO memory_write_ops(
                op_type, memory_id, status, step, payload, error, retry_count,
                created_at, updated_at, operation_key, entity_id
            ) VALUES (?, NULL, 'completed', 'canonical_committed', ?, NULL, 0, ?, ?, ?, ?)
            """,
            (
                operation_type,
                self._json_dump(payload),
                now,
                now,
                operation_key,
                entity_id,
            ),
        )

    async def add_source(
        self,
        *,
        owner_user_id: str,
        memory_item_id: str,
        revision_no: int,
        source: dict[str, Any],
    ) -> MemorySource:
        async with self._write_transaction(owner_user_id, memory_item_id) as db:
            if await self._get_item_db(db, owner_user_id, memory_item_id) is None:
                raise EvolvingMemoryNotFoundError()
            return await self._insert_source_db(
                db,
                owner_user_id=owner_user_id,
                memory_item_id=memory_item_id,
                revision_no=revision_no,
                source=source,
            )

    async def _insert_source_db(
        self,
        db: aiosqlite.Connection,
        *,
        owner_user_id: str,
        memory_item_id: str,
        revision_no: int,
        source: dict[str, Any],
    ) -> MemorySource:
        source_key = str(source.get("source_key") or "").strip()
        if not source_key:
            raise ValueError("source_key 不得为空")
        cursor = await db.execute(
            "SELECT * FROM memory_item_sources WHERE source_key = ?",
            (source_key,),
        )
        row = await cursor.fetchone()
        if row is not None:
            existing = self._row_to_source(row)
            if (
                existing.owner_user_id != owner_user_id
                or existing.memory_item_id != memory_item_id
            ):
                raise EvolvingMemoryIdempotencyError("source_key 已绑定到其他 owner/item")
            return existing
        created_at = utc_now_iso()
        source_id = str(source.get("source_id") or self._new_id("src"))
        await db.execute(
            """
            INSERT INTO memory_item_sources(
                source_id, source_key, owner_user_id, memory_item_id, revision_no,
                source_type, source_ref, document_id, session_id,
                message_start_id, message_end_id, content_snapshot,
                availability, metadata, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_id,
                source_key,
                owner_user_id,
                memory_item_id,
                revision_no,
                str(source.get("source_type") or "unknown"),
                source.get("source_ref"),
                source.get("document_id"),
                source.get("session_id"),
                source.get("message_start_id"),
                source.get("message_end_id"),
                source.get("content_snapshot"),
                str(source.get("availability") or SourceAvailability.AVAILABLE.value),
                self._json_dump(source.get("metadata")),
                created_at,
            ),
        )
        cursor = await db.execute(
            "SELECT * FROM memory_item_sources WHERE source_id = ?",
            (source_id,),
        )
        inserted = await cursor.fetchone()
        if inserted is None:
            raise EvolvingMemoryNotFoundError("source 创建失败")
        return self._row_to_source(inserted)

    async def list_sources(
        self, *, owner_user_id: str, memory_item_id: str
    ) -> list[MemorySource]:
        async with self._connect() as db:
            cursor = await db.execute(
                """
                SELECT * FROM memory_item_sources
                WHERE owner_user_id = ? AND memory_item_id = ?
                ORDER BY revision_no DESC, created_at DESC
                """,
                (owner_user_id, memory_item_id),
            )
            rows = await cursor.fetchall()
        return [self._row_to_source(row) for row in rows]

    def _row_to_source(self, row: aiosqlite.Row) -> MemorySource:
        return MemorySource(
            source_id=str(row["source_id"]),
            source_key=str(row["source_key"]),
            owner_user_id=str(row["owner_user_id"]),
            memory_item_id=str(row["memory_item_id"]),
            revision_no=int(row["revision_no"]),
            source_type=str(row["source_type"]),
            source_ref=row["source_ref"],
            document_id=int(row["document_id"]) if row["document_id"] is not None else None,
            session_id=row["session_id"],
            message_start_id=(
                int(row["message_start_id"])
                if row["message_start_id"] is not None
                else None
            ),
            message_end_id=(
                int(row["message_end_id"])
                if row["message_end_id"] is not None
                else None
            ),
            content_snapshot=row["content_snapshot"],
            availability=SourceAvailability(str(row["availability"])),
            metadata=self._json_object(row["metadata"]),
            created_at=str(row["created_at"]),
        )

    async def list_revisions(
        self, *, owner_user_id: str, memory_item_id: str
    ) -> list[MemoryRevision]:
        async with self._connect() as db:
            current = await self._get_item_db(db, owner_user_id, memory_item_id)
            if current is None:
                raise EvolvingMemoryNotFoundError()
            cursor = await db.execute(
                """
                SELECT * FROM memory_item_revisions
                WHERE memory_item_id = ?
                ORDER BY revision_no DESC
                """,
                (memory_item_id,),
            )
            rows = await cursor.fetchall()
        return [self._row_to_revision(row) for row in rows]

    def _row_to_revision(self, row: aiosqlite.Row) -> MemoryRevision:
        return MemoryRevision(
            revision_id=str(row["revision_id"]),
            memory_item_id=str(row["memory_item_id"]),
            owner_user_id=str(row["owner_user_id"]),
            revision_no=int(row["revision_no"]),
            operation=RevisionOperation(str(row["operation"])),
            content=str(row["content"]),
            content_hash=str(row["content_hash"]),
            structured_payload=self._json_object(row["structured_payload"]),
            base_version=int(row["base_version"]),
            actor_type=MemoryActorType(str(row["actor_type"])),
            actor_id=str(row["actor_id"]),
            reason=row["reason"],
            created_at=str(row["created_at"]),
        )

    async def _insert_relation_db(
        self,
        db: aiosqlite.Connection,
        *,
        owner_user_id: str,
        source_item_id: str,
        target_item_id: str,
        relation_type: MemoryRelationType,
        source_revision_no: int | None,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryRelation:
        relation_id = self._new_id("rel")
        created_at = utc_now_iso()
        await db.execute(
            """
            INSERT INTO memory_item_relations(
                relation_id, owner_user_id, source_item_id, target_item_id,
                relation_type, source_revision_no, metadata, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_item_id, target_item_id, relation_type) DO NOTHING
            """,
            (
                relation_id,
                owner_user_id,
                source_item_id,
                target_item_id,
                relation_type.value,
                source_revision_no,
                self._json_dump(metadata),
                created_at,
            ),
        )
        cursor = await db.execute(
            """
            SELECT * FROM memory_item_relations
            WHERE source_item_id = ? AND target_item_id = ? AND relation_type = ?
            """,
            (source_item_id, target_item_id, relation_type.value),
        )
        row = await cursor.fetchone()
        if row is None or str(row["owner_user_id"]) != owner_user_id:
            raise EvolvingMemoryAccessError("关系对象跨越 owner 边界")
        return self._row_to_relation(row)

    async def list_relations(
        self, *, owner_user_id: str, memory_item_id: str
    ) -> list[MemoryRelation]:
        async with self._connect() as db:
            cursor = await db.execute(
                """
                SELECT * FROM memory_item_relations
                WHERE owner_user_id = ? AND (source_item_id = ? OR target_item_id = ?)
                ORDER BY created_at DESC
                """,
                (owner_user_id, memory_item_id, memory_item_id),
            )
            rows = await cursor.fetchall()
        return [self._row_to_relation(row) for row in rows]

    def _row_to_relation(self, row: aiosqlite.Row) -> MemoryRelation:
        return MemoryRelation(
            relation_id=str(row["relation_id"]),
            owner_user_id=str(row["owner_user_id"]),
            source_item_id=str(row["source_item_id"]),
            target_item_id=str(row["target_item_id"]),
            relation_type=MemoryRelationType(str(row["relation_type"])),
            source_revision_no=(
                int(row["source_revision_no"])
                if row["source_revision_no"] is not None
                else None
            ),
            metadata=self._json_object(row["metadata"]),
            created_at=str(row["created_at"]),
        )

    async def create_conflict(
        self,
        *,
        context: MemoryAccessContext,
        left_item_id: str,
        right_item_id: str,
        expected_versions: dict[str, int],
        conflict_type: str,
        severity: ConflictSeverity = ConflictSeverity.MEDIUM,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryConflict:
        if left_item_id == right_item_id:
            raise ValueError("冲突对象两端不得相同")
        lock_key = "conflict:" + ":".join(sorted((left_item_id, right_item_id)))
        async with self._write_transaction(context.owner_user_id, lock_key) as db:
            left = await self._require_accessible_item_db(db, context, left_item_id)
            right = await self._require_accessible_item_db(db, context, right_item_id)
            for item in (left, right):
                expected = expected_versions.get(item.memory_item_id)
                if expected is None:
                    raise ValueError(f"缺少 expected_version: {item.memory_item_id}")
                self._assert_version(item, expected)
            now = utc_now_iso()
            for item in (left, right):
                await db.execute(
                    """
                    UPDATE memory_items
                    SET status = 'conflicted', version = version + 1, updated_at = ?
                    WHERE owner_user_id = ? AND memory_item_id = ? AND version = ?
                    """,
                    (now, context.owner_user_id, item.memory_item_id, item.version),
                )
                await self._refresh_fts_db(
                    db,
                    memory_item_id=item.memory_item_id,
                    owner_user_id=context.owner_user_id,
                    status=MemoryItemStatus.CONFLICTED,
                    content=item.content,
                    canonical_key=item.canonical_key,
                    item_type=item.item_type,
                )
            await self._insert_relation_db(
                db,
                owner_user_id=context.owner_user_id,
                source_item_id=left_item_id,
                target_item_id=right_item_id,
                relation_type=MemoryRelationType.CONFLICTS_WITH,
                source_revision_no=left.current_revision_no,
                metadata=metadata,
            )
            conflict_id = self._new_id("conflict")
            await db.execute(
                """
                INSERT INTO memory_conflicts(
                    conflict_id, owner_user_id, left_item_id, right_item_id,
                    conflict_type, severity, status, metadata, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)
                """,
                (
                    conflict_id,
                    context.owner_user_id,
                    left_item_id,
                    right_item_id,
                    conflict_type,
                    severity.value,
                    self._json_dump(metadata),
                    now,
                    now,
                ),
            )
            cursor = await db.execute(
                "SELECT * FROM memory_conflicts WHERE conflict_id = ? AND owner_user_id = ?",
                (conflict_id, context.owner_user_id),
            )
            row = await cursor.fetchone()
            if row is None:
                raise EvolvingMemoryNotFoundError("冲突记录创建失败")
            return self._row_to_conflict(row)

    async def list_conflicts(
        self,
        *,
        owner_user_id: str,
        status: ConflictStatus | None = None,
        limit: int = 100,
    ) -> list[MemoryConflict]:
        query = "SELECT * FROM memory_conflicts WHERE owner_user_id = ?"
        params: list[Any] = [owner_user_id]
        if status is not None:
            query += " AND status = ?"
            params.append(status.value)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        async with self._connect() as db:
            cursor = await db.execute(query, params)
            rows = await cursor.fetchall()
        return [self._row_to_conflict(row) for row in rows]

    async def get_conflict(
        self,
        *,
        owner_user_id: str,
        conflict_id: str,
    ) -> MemoryConflict | None:
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT * FROM memory_conflicts WHERE owner_user_id = ? AND conflict_id = ?",
                (owner_user_id, conflict_id),
            )
            row = await cursor.fetchone()
        return self._row_to_conflict(row) if row else None

    async def require_open_conflict(
        self,
        *,
        context: MemoryAccessContext,
        conflict_id: str,
        expected_versions: dict[str, int],
    ) -> MemoryConflict:
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT * FROM memory_conflicts WHERE owner_user_id = ? AND conflict_id = ?",
                (context.owner_user_id, conflict_id),
            )
            row = await cursor.fetchone()
            if row is None:
                raise EvolvingMemoryNotFoundError("冲突记录不存在")
            conflict = self._row_to_conflict(row)
            if conflict.status != ConflictStatus.OPEN:
                raise EvolvingMemoryAccessError("冲突记录已锁定")
            for item_id in (conflict.left_item_id, conflict.right_item_id):
                item = await self._require_accessible_item_db(db, context, item_id)
                expected = expected_versions.get(item_id)
                if expected is None:
                    raise ValueError(f"缺少 expected_version: {item_id}")
                self._assert_version(item, expected)
            return conflict

    async def resolve_conflict(
        self,
        *,
        context: MemoryAccessContext,
        conflict_id: str,
        action: str,
        expected_versions: dict[str, int],
        operation_key: str,
        resolved_by: str,
        resolution_note: str | None = None,
        survivor_item_id: str | None = None,
        content: str | None = None,
    ) -> tuple[MemoryConflict, MemoryItem | None]:
        operation_request = {
            "conflict_id": conflict_id,
            "action": action,
            "expected_versions": expected_versions,
            "resolved_by": resolved_by,
            "resolution_note": resolution_note,
            "survivor_item_id": survivor_item_id,
            "content": content,
        }
        lock_key = f"conflict-resolve:{conflict_id}"
        async with self._write_transaction(context.owner_user_id, lock_key) as db:
            replay = await self._load_operation_db(
                db,
                operation_key=operation_key,
                owner_user_id=context.owner_user_id,
                operation_type="conflict_resolve",
                request_payload=operation_request,
            )
            if replay is not None:
                replay_row = await (
                    await db.execute(
                        "SELECT * FROM memory_conflicts WHERE owner_user_id = ? AND conflict_id = ?",
                        (context.owner_user_id, conflict_id),
                    )
                ).fetchone()
                if replay_row is None:
                    raise EvolvingMemoryIdempotencyError("幂等记录引用的冲突不存在")
                result_item_id = str(replay.get("result_item_id") or "")
                replay_item = (
                    await self._get_item_db(db, context.owner_user_id, result_item_id)
                    if result_item_id
                    else None
                )
                return self._row_to_conflict(replay_row), replay_item
            row = await (
                await db.execute(
                    "SELECT * FROM memory_conflicts WHERE owner_user_id = ? AND conflict_id = ?",
                    (context.owner_user_id, conflict_id),
                )
            ).fetchone()
            if row is None:
                raise EvolvingMemoryNotFoundError("冲突记录不存在")
            conflict = self._row_to_conflict(row)
            if conflict.status != ConflictStatus.OPEN:
                raise EvolvingMemoryAccessError("冲突记录已锁定")

            items = {
                item_id: await self._require_accessible_item_db(db, context, item_id)
                for item_id in (conflict.left_item_id, conflict.right_item_id)
            }
            for item_id, item in items.items():
                expected = expected_versions.get(item_id)
                if expected is None:
                    raise ValueError(f"缺少 expected_version: {item_id}")
                self._assert_version(item, expected)

            now = utc_now_iso()
            result_item: MemoryItem | None = None
            resolved_status = ConflictStatus.DISMISSED
            if action == "merge":
                survivor_id = survivor_item_id or conflict.left_item_id
                if survivor_id not in items:
                    raise ValueError("survivor_memory_item_id 必须属于冲突两端")
                source_id = (
                    conflict.right_item_id
                    if survivor_id == conflict.left_item_id
                    else conflict.left_item_id
                )
                survivor = items[survivor_id]
                source = items[source_id]
                merged_content = (content or f"{survivor.content}\n{source.content}").strip()
                if not merged_content:
                    raise ValueError("merge content 不得为空")
                merged_key = " ".join(merged_content.casefold().split())
                survivor_revision = survivor.current_revision_no + 1
                survivor_hash = self.content_hash(merged_content)
                await self._insert_revision_db(
                    db,
                    item=survivor,
                    revision_no=survivor_revision,
                    operation=RevisionOperation.MERGE,
                    content=merged_content,
                    content_hash=survivor_hash,
                    structured_payload=survivor.structured_payload,
                    base_version=survivor.version,
                    actor_type=MemoryActorType.ADMIN,
                    actor_id=resolved_by,
                    reason=resolution_note,
                )
                cursor = await db.execute(
                    """
                    UPDATE memory_items
                    SET canonical_key = ?, content_hash = ?, current_revision_no = ?,
                        version = version + 1, status = 'active', index_status = 'pending',
                        index_error = NULL, updated_at = ?
                    WHERE owner_user_id = ? AND memory_item_id = ? AND version = ?
                    """,
                    (
                        merged_key,
                        survivor_hash,
                        survivor_revision,
                        now,
                        context.owner_user_id,
                        survivor_id,
                        survivor.version,
                    ),
                )
                if cursor.rowcount != 1:
                    latest = await self._get_item_db(db, context.owner_user_id, survivor_id)
                    raise EvolvingMemoryVersionConflictError(
                        survivor_id,
                        survivor.version,
                        latest.version if latest else -1,
                    )
                await self._refresh_fts_db(
                    db,
                    memory_item_id=survivor_id,
                    owner_user_id=context.owner_user_id,
                    status=MemoryItemStatus.ACTIVE,
                    content=merged_content,
                    canonical_key=merged_key,
                    item_type=survivor.item_type,
                )

                source_revision = source.current_revision_no + 1
                await self._insert_revision_db(
                    db,
                    item=source,
                    revision_no=source_revision,
                    operation=RevisionOperation.MERGE,
                    content=source.content,
                    content_hash=source.content_hash,
                    structured_payload=source.structured_payload,
                    base_version=source.version,
                    actor_type=MemoryActorType.ADMIN,
                    actor_id=resolved_by,
                    reason=resolution_note,
                )
                cursor = await db.execute(
                    """
                    UPDATE memory_items
                    SET status = 'superseded', current_revision_no = ?,
                        version = version + 1, index_status = 'pending',
                        index_error = NULL, updated_at = ?
                    WHERE owner_user_id = ? AND memory_item_id = ? AND version = ?
                    """,
                    (
                        source_revision,
                        now,
                        context.owner_user_id,
                        source_id,
                        source.version,
                    ),
                )
                if cursor.rowcount != 1:
                    latest = await self._get_item_db(db, context.owner_user_id, source_id)
                    raise EvolvingMemoryVersionConflictError(
                        source_id,
                        source.version,
                        latest.version if latest else -1,
                    )
                await self._refresh_fts_db(
                    db,
                    memory_item_id=source_id,
                    owner_user_id=context.owner_user_id,
                    status=MemoryItemStatus.SUPERSEDED,
                    content=source.content,
                    canonical_key=source.canonical_key,
                    item_type=source.item_type,
                )
                await self._insert_relation_db(
                    db,
                    owner_user_id=context.owner_user_id,
                    source_item_id=source_id,
                    target_item_id=survivor_id,
                    relation_type=MemoryRelationType.MERGED_INTO,
                    source_revision_no=source_revision,
                    metadata={"operation_key": operation_key, "conflict_id": conflict_id},
                )
                result_item = await self._get_item_db(db, context.owner_user_id, survivor_id)
                resolved_status = ConflictStatus.RESOLVED
            elif action in {"supersede_left", "supersede_right"}:
                if action == "supersede_left":
                    old_item = items[conflict.left_item_id]
                    replacement = items[conflict.right_item_id]
                else:
                    old_item = items[conflict.right_item_id]
                    replacement = items[conflict.left_item_id]
                for item, new_status in (
                    (old_item, MemoryItemStatus.SUPERSEDED),
                    (replacement, MemoryItemStatus.ACTIVE),
                ):
                    revision_no = item.current_revision_no + 1
                    await self._insert_revision_db(
                        db,
                        item=item,
                        revision_no=revision_no,
                        operation=RevisionOperation.SUPERSEDE,
                        content=item.content,
                        content_hash=item.content_hash,
                        structured_payload=item.structured_payload,
                        base_version=item.version,
                        actor_type=MemoryActorType.ADMIN,
                        actor_id=resolved_by,
                        reason=resolution_note,
                    )
                    cursor = await db.execute(
                        """
                        UPDATE memory_items
                        SET status = ?, current_revision_no = ?, version = version + 1,
                            index_status = 'pending', index_error = NULL, updated_at = ?
                        WHERE owner_user_id = ? AND memory_item_id = ? AND version = ?
                        """,
                        (
                            new_status.value,
                            revision_no,
                            now,
                            context.owner_user_id,
                            item.memory_item_id,
                            item.version,
                        ),
                    )
                    if cursor.rowcount != 1:
                        latest = await self._get_item_db(
                            db,
                            context.owner_user_id,
                            item.memory_item_id,
                        )
                        raise EvolvingMemoryVersionConflictError(
                            item.memory_item_id,
                            item.version,
                            latest.version if latest else -1,
                        )
                    await self._refresh_fts_db(
                        db,
                        memory_item_id=item.memory_item_id,
                        owner_user_id=context.owner_user_id,
                        status=new_status,
                        content=item.content,
                        canonical_key=item.canonical_key,
                        item_type=item.item_type,
                    )
                await self._insert_relation_db(
                    db,
                    owner_user_id=context.owner_user_id,
                    source_item_id=replacement.memory_item_id,
                    target_item_id=old_item.memory_item_id,
                    relation_type=MemoryRelationType.SUPERSEDES,
                    source_revision_no=replacement.current_revision_no + 1,
                    metadata={"operation_key": operation_key, "conflict_id": conflict_id},
                )
                result_item = await self._get_item_db(
                    db,
                    context.owner_user_id,
                    replacement.memory_item_id,
                )
                resolved_status = ConflictStatus.RESOLVED
            elif action != "dismiss":
                raise ValueError(
                    "action 必须是 merge、supersede_left、supersede_right 或 dismiss"
                )

            cursor = await db.execute(
                """
                UPDATE memory_conflicts
                SET status = ?, resolution_action = ?, resolved_by = ?,
                    resolution_note = ?, updated_at = ?, resolved_at = ?
                WHERE owner_user_id = ? AND conflict_id = ? AND status = 'open'
                """,
                (
                    resolved_status.value,
                    action,
                    resolved_by,
                    resolution_note,
                    now,
                    now,
                    context.owner_user_id,
                    conflict_id,
                ),
            )
            if cursor.rowcount != 1:
                raise EvolvingMemoryAccessError("冲突记录已锁定")
            updated_row = await (
                await db.execute(
                    "SELECT * FROM memory_conflicts WHERE owner_user_id = ? AND conflict_id = ?",
                    (context.owner_user_id, conflict_id),
                )
            ).fetchone()
            if updated_row is None:
                raise EvolvingMemoryNotFoundError("冲突记录更新失败")
            await self._record_operation_db(
                db,
                operation_key=operation_key,
                action=None,
                owner_user_id=context.owner_user_id,
                entity_id=conflict_id,
                version=result_item.version if result_item is not None else 0,
                request_payload=operation_request,
                op_type="conflict_resolve",
                extra={
                    "conflict_id": conflict_id,
                    "result_item_id": (
                        result_item.memory_item_id if result_item is not None else None
                    ),
                },
            )
            return self._row_to_conflict(updated_row), result_item

    async def resolve_conflict_record(
        self,
        *,
        owner_user_id: str,
        conflict_id: str,
        status: ConflictStatus,
        resolution_action: str,
        resolved_by: str,
        resolution_note: str | None = None,
    ) -> MemoryConflict:
        async with self._write_transaction(owner_user_id, f"conflict-resolve:{conflict_id}") as db:
            cursor = await db.execute(
                "SELECT * FROM memory_conflicts WHERE owner_user_id = ? AND conflict_id = ?",
                (owner_user_id, conflict_id),
            )
            row = await cursor.fetchone()
            if row is None:
                raise EvolvingMemoryNotFoundError("冲突记录不存在")
            current = self._row_to_conflict(row)
            if current.status != ConflictStatus.OPEN:
                raise EvolvingMemoryAccessError("冲突记录已锁定")
            now = utc_now_iso()
            await db.execute(
                """
                UPDATE memory_conflicts
                SET status = ?, resolution_action = ?, resolved_by = ?,
                    resolution_note = ?, updated_at = ?, resolved_at = ?
                WHERE owner_user_id = ? AND conflict_id = ? AND status = 'open'
                """,
                (
                    status.value,
                    resolution_action,
                    resolved_by,
                    resolution_note,
                    now,
                    now,
                    owner_user_id,
                    conflict_id,
                ),
            )
            cursor = await db.execute(
                "SELECT * FROM memory_conflicts WHERE owner_user_id = ? AND conflict_id = ?",
                (owner_user_id, conflict_id),
            )
            updated_row = await cursor.fetchone()
            if updated_row is None:
                raise EvolvingMemoryNotFoundError("冲突记录更新失败")
            return self._row_to_conflict(updated_row)

    async def maintenance_status(self) -> dict[str, Any]:
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT index_status, COUNT(*) AS total FROM memory_items GROUP BY index_status"
            )
            index_counts = {str(row["index_status"]): int(row["total"]) for row in await cursor.fetchall()}
            cursor = await db.execute(
                "SELECT MAX(updated_at) FROM memory_items WHERE index_status = 'current'"
            )
            last_success_row = await cursor.fetchone()
            cursor = await db.execute(
                """
                SELECT index_error FROM memory_items
                WHERE index_status = 'needs_repair' AND index_error IS NOT NULL
                ORDER BY updated_at DESC LIMIT 1
                """
            )
            last_error_row = await cursor.fetchone()
            cursor = await db.execute("SELECT COUNT(*) FROM memory_items")
            total_items = int((await cursor.fetchone())[0])
            cursor = await db.execute(
                """
                SELECT
                    SUM(CASE WHEN has_available = 1 THEN 1 ELSE 0 END) AS covered,
                    SUM(CASE WHEN has_available = 0 AND has_partial = 1 THEN 1 ELSE 0 END) AS partial
                FROM (
                    SELECT mi.memory_item_id,
                           MAX(CASE WHEN src.availability = 'available' THEN 1 ELSE 0 END) AS has_available,
                           MAX(CASE WHEN src.availability = 'partial' THEN 1 ELSE 0 END) AS has_partial
                    FROM memory_items mi
                    LEFT JOIN memory_item_sources src ON src.memory_item_id = mi.memory_item_id
                    GROUP BY mi.memory_item_id
                ) coverage
                """
            )
            coverage_row = await cursor.fetchone()
            covered_items = int(coverage_row["covered"] or 0) if coverage_row else 0
            partial_items = int(coverage_row["partial"] or 0) if coverage_row else 0
            cursor = await db.execute(
                "SELECT value FROM migration_status WHERE key = ?",
                ("evolving_memory_key_facts_v1",),
            )
            migration_row = await cursor.fetchone()
            migration = self._json_object(migration_row["value"]) if migration_row else {}
            cursor = await db.execute(
                """
                SELECT COUNT(*) FROM memory_owners
                WHERE status = 'active' AND json_valid(metadata)
                  AND CAST(json_extract(metadata, '$.legacy_isolated') AS INTEGER) = 1
                """
            )
            unresolved_owner_count = int((await cursor.fetchone())[0])
            total_documents = 0
            cursor = await db.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'documents'"
            )
            if await cursor.fetchone() is not None:
                total_documents = int((await (await db.execute("SELECT COUNT(*) FROM documents")).fetchone())[0])
        pending_count = index_counts.get(IndexStatus.PENDING.value, 0)
        repair_count = index_counts.get(IndexStatus.NEEDS_REPAIR.value, 0)
        index_state = "degraded" if repair_count else ("repairing" if pending_count else "synced")
        migration_state = str(migration.get("status") or "idle")
        if migration_state == "complete":
            migration_state = "completed"
        unavailable_items = max(0, total_items - covered_items - partial_items)
        return {
            "migration": {
                "state": migration_state,
                "processed": int(migration.get("cursor", 0) or 0),
                "total": total_documents,
                "created": int(migration.get("created", 0) or 0),
                "deduped": int(migration.get("dedup", 0) or 0),
                "skipped": int(migration.get("skipped", 0) or 0),
                "conflicted": int(migration.get("conflicted", 0) or 0),
                "errors": int(migration.get("errors", 0) or 0),
                "unresolved_owner_count": unresolved_owner_count,
            },
            "index": {
                "state": index_state,
                "synced_count": index_counts.get(IndexStatus.CURRENT.value, 0),
                "pending_count": pending_count,
                "needs_repair_count": repair_count,
                "disabled_count": index_counts.get(IndexStatus.DISABLED.value, 0),
                "last_success_at": last_success_row[0] if last_success_row else None,
                "last_error": last_error_row[0] if last_error_row else None,
            },
            "sources": {
                "total_items": total_items,
                "covered_items": covered_items,
                "partial_items": partial_items,
                "unavailable_items": unavailable_items,
                "coverage_ratio": covered_items / total_items if total_items else 0.0,
            },
        }

    async def retry_index_items(
        self,
        *,
        context: MemoryAccessContext,
        expected_versions: dict[str, int],
    ) -> int:
        item_ids = tuple(dict.fromkeys(expected_versions))
        if not item_ids:
            raise ValueError("至少提供一个待重试对象")
        lock_key = "index-retry:" + ":".join(sorted(item_ids))
        async with self._write_transaction(context.owner_user_id, lock_key) as db:
            for item_id in item_ids:
                item = await self._require_accessible_item_db(db, context, item_id)
                self._assert_version(item, expected_versions[item_id])
            placeholders = ",".join("?" for _ in item_ids)
            cursor = await db.execute(
                f"""
                UPDATE memory_items
                SET index_status = 'pending', index_error = NULL, updated_at = ?
                WHERE owner_user_id = ? AND memory_item_id IN ({placeholders})
                """,
                (utc_now_iso(), context.owner_user_id, *item_ids),
            )
            return int(cursor.rowcount)

    def _row_to_conflict(self, row: aiosqlite.Row) -> MemoryConflict:
        return MemoryConflict(
            conflict_id=str(row["conflict_id"]),
            owner_user_id=str(row["owner_user_id"]),
            left_item_id=str(row["left_item_id"]),
            right_item_id=str(row["right_item_id"]),
            conflict_type=str(row["conflict_type"]),
            severity=ConflictSeverity(str(row["severity"])),
            status=ConflictStatus(str(row["status"])),
            resolution_action=row["resolution_action"],
            resolved_by=row["resolved_by"],
            resolution_note=row["resolution_note"],
            metadata=self._json_object(row["metadata"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            resolved_at=row["resolved_at"],
        )

    async def find_duplicate_candidates(
        self,
        *,
        context: MemoryAccessContext,
        content: str,
        canonical_key: str,
        limit: int = 10,
    ) -> list[DuplicateCandidate]:
        digest = self.content_hash(content)
        where, params = self._access_clause(context)
        active_filter = "mi.status IN ('active', 'conflicted')"
        candidates: dict[str, DuplicateCandidate] = {}
        async with self._connect() as db:
            cursor = await db.execute(
                f"""
                SELECT mi.*, r.content AS revision_content,
                       r.structured_payload AS revision_structured_payload
                FROM memory_items mi
                JOIN memory_item_revisions r
                  ON r.memory_item_id = mi.memory_item_id
                 AND r.revision_no = mi.current_revision_no
                WHERE {where} AND {active_filter}
                  AND (mi.content_hash = ? OR mi.canonical_key = ?)
                ORDER BY CASE WHEN mi.content_hash = ? THEN 0 ELSE 1 END,
                         mi.updated_at DESC
                LIMIT ?
                """,
                (*params, digest, canonical_key, digest, max(1, min(limit, 100))),
            )
            rows = await cursor.fetchall()
            for row in rows:
                item = self._row_to_item(row)
                match_type = "exact" if item.content_hash == digest else "canonical"
                score = 1.0 if match_type == "exact" else 0.95
                candidates[item.memory_item_id] = DuplicateCandidate(
                    item=item,
                    match_type=match_type,
                    score=float(score),
                )

            if len(candidates) < limit:
                fts_query = self._fts_query(content)
                if fts_query:
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
                              AND {where} AND {active_filter}
                            ORDER BY bm25_score ASC
                            LIMIT ?
                            """,
                            (fts_query, *params, max(1, min(limit, 100))),
                        )
                        fts_rows = await cursor.fetchall()
                    except aiosqlite.Error:
                        fts_rows = []
                    for rank, row in enumerate(fts_rows):
                        item = self._row_to_item(row)
                        if item.memory_item_id in candidates:
                            continue
                        score = max(0.1, 0.8 - rank * 0.05)
                        candidates[item.memory_item_id] = DuplicateCandidate(
                            item=item,
                            match_type="fts",
                            score=float(score),
                        )
        return list(candidates.values())[:limit]

    @staticmethod
    def _fts_query(content: str) -> str:
        tokens = [token for token in " ".join(content.split()).split(" ") if token]
        safe_tokens = []
        for token in tokens[:32]:
            cleaned = token.replace('"', '""').strip()
            if cleaned:
                safe_tokens.append(f'"{cleaned}"')
        return " OR ".join(safe_tokens)

    async def set_index_status(
        self,
        *,
        owner_user_id: str,
        memory_item_id: str,
        status: IndexStatus,
        error: str | None = None,
        current_document_id: int | None = None,
    ) -> None:
        async with self._write_transaction(owner_user_id, memory_item_id) as db:
            cursor = await db.execute(
                """
                UPDATE memory_items
                SET index_status = ?, index_error = ?,
                    current_document_id = COALESCE(?, current_document_id), updated_at = ?
                WHERE owner_user_id = ? AND memory_item_id = ?
                """,
                (
                    status.value,
                    error[:2000] if error else None,
                    current_document_id,
                    utc_now_iso(),
                    owner_user_id,
                    memory_item_id,
                ),
            )
            if cursor.rowcount != 1:
                raise EvolvingMemoryNotFoundError()

    async def get_generation_token(self, owner_user_id: str) -> tuple[int, int, str]:
        """Return a cheap owner-scoped mutation token for retrieval cache isolation."""
        async with self._connect() as db:
            cursor = await db.execute(
                """
                SELECT COUNT(*), COALESCE(SUM(version), 0), COALESCE(MAX(updated_at), '')
                FROM memory_items
                WHERE owner_user_id = ?
                """,
                (owner_user_id,),
            )
            row = await cursor.fetchone()
        if row is None:
            return (0, 0, "")
        return (int(row[0]), int(row[1]), str(row[2]))

    async def get_migration_checkpoint(self, key: str) -> dict[str, Any]:
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT value FROM migration_status WHERE key = ?",
                (key,),
            )
            row = await cursor.fetchone()
        return self._json_object(row[0]) if row else {}

    async def set_migration_checkpoint(self, key: str, value: dict[str, Any]) -> None:
        async with self._write_transaction("migration", key) as db:
            await db.execute(
                """
                INSERT INTO migration_status(key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, self._json_dump(value), utc_now_iso()),
            )

    async def backfill_batch(
        self,
        *,
        candidates: list[dict[str, Any]],
        checkpoint_key: str,
        checkpoint: dict[str, Any],
    ) -> dict[str, int]:
        """Insert deterministic backfill candidates and checkpoint in one IMMEDIATE txn."""
        stats = {"created": 0, "dedup": 0, "conflicted": 0, "errors": 0}
        async with self._write_transaction("migration", checkpoint_key) as db:
            for candidate in candidates:
                try:
                    owner_id = str(candidate["owner_user_id"])
                    if await self._get_owner_db(db, owner_id) is None:
                        now = utc_now_iso()
                        await db.execute(
                            """
                            INSERT INTO memory_owners(
                                owner_user_id, display_name, status, metadata, created_at, updated_at
                            ) VALUES (?, NULL, 'active', ?, ?, ?)
                            """,
                            (
                                owner_id,
                                self._json_dump(candidate.get("owner_metadata")),
                                now,
                                now,
                            ),
                        )
                    candidate_item_id = str(candidate["memory_item_id"])
                    cursor = await db.execute(
                        "SELECT owner_user_id FROM memory_items WHERE memory_item_id = ?",
                        (candidate_item_id,),
                    )
                    existing_row = await cursor.fetchone()
                    if existing_row is not None:
                        if str(existing_row["owner_user_id"]) == owner_id:
                            stats["dedup"] += 1
                        else:
                            stats["conflicted"] += 1
                        continue
                    await self._insert_item_db(
                        db,
                        memory_item_id=str(candidate["memory_item_id"]),
                        owner_user_id=owner_id,
                        scope=MemoryScope(str(candidate["scope"])),
                        session_id=candidate.get("session_id"),
                        persona_id=candidate.get("persona_id"),
                        item_type=str(candidate.get("item_type") or "fact"),
                        canonical_key=str(candidate["canonical_key"]),
                        content=str(candidate["content"]),
                        structured_payload=dict(candidate.get("structured_payload") or {}),
                        importance=float(candidate.get("importance", 0.5)),
                        confidence=float(candidate.get("confidence", 0.7)),
                        group_safe=bool(candidate.get("group_safe", False)),
                        actor_type=MemoryActorType.MIGRATION,
                        actor_id="legacy-key-facts-backfill",
                        reason="deterministic key_facts backfill",
                        revision_operation=RevisionOperation.BACKFILL,
                    )
                    source = candidate.get("source")
                    if isinstance(source, dict):
                        await self._insert_source_db(
                            db,
                            owner_user_id=owner_id,
                            memory_item_id=str(candidate["memory_item_id"]),
                            revision_no=1,
                            source=source,
                        )
                    await self._record_operation_db(
                        db,
                        operation_key=str(candidate["operation_key"]),
                        action=MemoryAction.CREATE,
                        owner_user_id=owner_id,
                        entity_id=str(candidate["memory_item_id"]),
                        version=1,
                        request_payload={
                            key: value
                            for key, value in candidate.items()
                            if key != "operation_key"
                        },
                        op_type="backfill",
                    )
                    stats["created"] += 1
                except Exception:
                    stats["errors"] += 1
                    raise
            merged_checkpoint = dict(checkpoint)
            for key, value in stats.items():
                merged_checkpoint[key] = int(checkpoint.get(key, 0) or 0) + value
            await db.execute(
                """
                INSERT INTO migration_status(key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (checkpoint_key, self._json_dump(merged_checkpoint), utc_now_iso()),
            )
        return stats


__all__ = ["EvolvingMemoryStore"]
